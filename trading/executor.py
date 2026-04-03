"""
DEX swap executor for Base chain.
Supports Uniswap V3, V4, and Aerodrome.
Auto-detects pool version from DexScreener labels.
Uses dynamic slippage (starts low, increases on retry).
"""

import time
from web3 import Web3
from eth_abi import encode

from config.constants import (
    UNISWAP_V3_ROUTER, AERODROME_ROUTER, WETH_ADDRESS,
    MAX_BUY_SLIPPAGE_PCT, MAX_SELL_SLIPPAGE_PCT, TRADING_ENABLED,
)
from trading.wallet import (
    get_web3, get_account, get_address, sign_and_send, wait_for_receipt,
    get_token_balance, get_token_decimals, get_weth_balance,
)
from monitoring.logger import write_log

MAX_ATTEMPTS = 3
CONFIRMATION_TIMEOUT = 30

# V4 swap router on Base (discovered from on-chain tx analysis)
UNISWAP_V4_ROUTER = '0xE54Bf6E506918B5B7fFb73Db50C7E6Aa7E84693C'

# V4 swap function: swap((uint8,address,address,address,uint24,int24,address,bytes)[],address,uint256,uint256)
V4_SWAP_SELECTOR = bytes.fromhex('e6cb474f')

# WETH ABI for wrapping/unwrapping
WETH_ABI = [
    {"inputs": [], "name": "deposit", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "wad", "type": "uint256"}], "name": "withdraw", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

# Uniswap V3 SwapRouter02 ABI (exactInputSingle)
UNISWAP_V3_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "recipient", "type": "address"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
            "name": "params",
            "type": "tuple",
        }],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
]

# ERC20 approve ABI
ERC20_APPROVE_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def _detect_pool_version(token_address: str, dex_id: str = '') -> str:
    """
    Detect if a token trades on V3 or V4 by checking DexScreener labels.
    Returns 'v4', 'v3', or 'aerodrome'.
    """
    if 'aerodrome' in dex_id.lower():
        return 'aerodrome'

    try:
        import requests
        resp = requests.get(
            f'https://api.dexscreener.com/latest/dex/tokens/{token_address}',
            timeout=10,
        )
        resp.raise_for_status()
        pairs = resp.json().get('pairs', [])

        # Find the best Base pair
        base_pairs = [p for p in pairs if p.get('chainId') == 'base']
        if not base_pairs:
            return 'v3'

        # Sort by liquidity
        base_pairs.sort(key=lambda p: float((p.get('liquidity') or {}).get('usd', 0) or 0), reverse=True)
        best = base_pairs[0]
        labels = best.get('labels', [])

        if 'v4' in labels:
            write_log(f'EXECUTOR | {token_address[:12]}... detected as V4 pool')
            return 'v4'
        return 'v3'
    except Exception:
        return 'v3'


def buy_token(token_address: str, eth_amount: float, dex_id: str = 'uniswap',
              max_slippage_pct: float = MAX_BUY_SLIPPAGE_PCT) -> dict:
    """Buy a token with ETH. Auto-detects V3 vs V4."""
    if not TRADING_ENABLED:
        return {'success': False, 'error': 'Trading disabled'}

    w3 = get_web3()
    account = get_account()
    if not account:
        return {'success': False, 'error': 'No trading account configured'}

    amount_in_wei = w3.to_wei(eth_amount, 'ether')
    pool_version = _detect_pool_version(token_address, dex_id)

    for attempt in range(MAX_ATTEMPTS):
        slippage_pct = min(max_slippage_pct * (0.5 + 0.25 * attempt), max_slippage_pct)

        try:
            if pool_version == 'v4':
                result = _buy_v4(w3, account, token_address, amount_in_wei, slippage_pct)
            elif pool_version == 'aerodrome':
                result = _buy_v3(w3, account, token_address, amount_in_wei, slippage_pct)
            else:
                result = _buy_v3(w3, account, token_address, amount_in_wei, slippage_pct)

            if result.get('success'):
                return result

            # If V3 fails, try V4 (pool might have migrated)
            if pool_version == 'v3' and attempt == 1:
                write_log(f'EXECUTOR | V3 failed, trying V4 fallback')
                pool_version = 'v4'

            write_log(f'EXECUTOR | Buy attempt {attempt + 1}/{MAX_ATTEMPTS} failed ({pool_version}): {result.get("error")}')

        except Exception as e:
            write_log(f'EXECUTOR | Buy attempt {attempt + 1}/{MAX_ATTEMPTS} error: {e}')

        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(2 * (attempt + 1))

    return {'success': False, 'error': f'All {MAX_ATTEMPTS} buy attempts failed'}


def sell_token(token_address: str, token_amount: int | None = None,
               sell_pct: float = 100, dex_id: str = 'uniswap',
               max_slippage_pct: float = MAX_SELL_SLIPPAGE_PCT) -> dict:
    """Sell a token for ETH. Auto-detects V3 vs V4."""
    if not TRADING_ENABLED:
        return {'success': False, 'error': 'Trading disabled'}

    w3 = get_web3()
    account = get_account()
    if not account:
        return {'success': False, 'error': 'No trading account configured'}

    if token_amount is None:
        balance = get_token_balance(token_address)
        if balance <= 0:
            return {'success': False, 'error': 'No token balance'}
        token_amount = int(balance * sell_pct / 100)

    if token_amount <= 0:
        return {'success': False, 'error': 'Nothing to sell'}

    pool_version = _detect_pool_version(token_address, dex_id)

    for attempt in range(MAX_ATTEMPTS):
        slippage_pct = min(max_slippage_pct * (0.5 + 0.25 * attempt), max_slippage_pct)

        try:
            if pool_version == 'v4':
                router = UNISWAP_V4_ROUTER
            else:
                router = UNISWAP_V3_ROUTER
            _ensure_approval(w3, account, token_address, router, token_amount)

            if pool_version == 'v4':
                result = _sell_v4(w3, account, token_address, token_amount, slippage_pct)
            else:
                result = _sell_v3(w3, account, token_address, token_amount, slippage_pct)

            if result.get('success'):
                return result

            if pool_version == 'v3' and attempt == 1:
                write_log(f'EXECUTOR | V3 sell failed, trying V4 fallback')
                pool_version = 'v4'

            write_log(f'EXECUTOR | Sell attempt {attempt + 1}/{MAX_ATTEMPTS} failed ({pool_version}): {result.get("error")}')

        except Exception as e:
            write_log(f'EXECUTOR | Sell attempt {attempt + 1}/{MAX_ATTEMPTS} error: {e}')

        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(2 * (attempt + 1))

    return {'success': False, 'error': f'All {MAX_ATTEMPTS} sell attempts failed'}


# === V4 Swap Implementation ===

def _wrap_eth(w3: Web3, account, amount_wei: int) -> str:
    """Wrap ETH to WETH."""
    weth = w3.eth.contract(address=Web3.to_checksum_address(WETH_ADDRESS), abi=WETH_ABI)
    tx = weth.functions.deposit().build_transaction({
        'from': account.address,
        'value': amount_wei,
        'gas': 60_000,
        'maxFeePerGas': w3.eth.gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei(0.001, 'gwei'),
        'nonce': w3.eth.get_transaction_count(account.address),
        'chainId': 8453,
    })
    tx_hash = sign_and_send(tx)
    wait_for_receipt(tx_hash, timeout=30)
    write_log(f'EXECUTOR | Wrapped {float(w3.from_wei(amount_wei, "ether")):.4f} ETH to WETH')
    return tx_hash


def _unwrap_weth(w3: Web3, account, amount_wei: int) -> str:
    """Unwrap WETH to ETH."""
    weth = w3.eth.contract(address=Web3.to_checksum_address(WETH_ADDRESS), abi=WETH_ABI)
    tx = weth.functions.withdraw(amount_wei).build_transaction({
        'from': account.address,
        'value': 0,
        'gas': 60_000,
        'maxFeePerGas': w3.eth.gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei(0.001, 'gwei'),
        'nonce': w3.eth.get_transaction_count(account.address),
        'chainId': 8453,
    })
    tx_hash = sign_and_send(tx)
    wait_for_receipt(tx_hash, timeout=30)
    return tx_hash


def _build_v4_swap_data(token_in: str, token_out: str, recipient: str,
                        amount_in: int, min_amount_out: int = 0) -> bytes:
    """
    Build calldata for the V4 swap router.
    Function: swap((uint8,address,address,address,uint24,int24,address,bytes)[],address,uint256,uint256)

    Step tuple: (type, token0, token1, feeOrParam, fee, tickSpacing, hookAddress, hookData)
    - type 2 = exact input single swap
    - fee 100 = 0.01% (most common for V4 meme coins on Base)
    - tickSpacing 1
    """
    # Encode the step tuple
    step = (
        2,                                                    # swap type: exact input
        Web3.to_checksum_address(token_in),                   # token0
        Web3.to_checksum_address(token_out),                  # token1
        '0x0000000000000000000000000000000000000000',          # feeOrParam (zero address)
        100,                                                   # fee: 0.01%
        1,                                                     # tickSpacing
        '0x0000000000000000000000000000000000000000',          # hookAddress (none)
        b'',                                                   # hookData (empty)
    )

    # Encode the full function call
    encoded_params = encode(
        ['(uint8,address,address,address,uint24,int24,address,bytes)[]', 'address', 'uint256', 'uint256'],
        [[step], Web3.to_checksum_address(recipient), amount_in, min_amount_out]
    )

    return V4_SWAP_SELECTOR + encoded_params


def _buy_v4(w3: Web3, account, token_address: str,
            amount_in_wei: int, slippage_pct: float) -> dict:
    """Execute a buy on Uniswap V4 (ETH → WETH → Token)."""
    token_before = get_token_balance(token_address)

    # Step 1: Wrap ETH to WETH
    _wrap_eth(w3, account, amount_in_wei)

    # Step 2: Approve WETH for V4 router
    _ensure_approval(w3, account, WETH_ADDRESS, UNISWAP_V4_ROUTER, amount_in_wei)

    # Step 3: Execute V4 swap (WETH → Token)
    calldata = _build_v4_swap_data(
        token_in=WETH_ADDRESS,
        token_out=token_address,
        recipient=account.address,
        amount_in=amount_in_wei,
        min_amount_out=0,
    )

    tx = {
        'from': account.address,
        'to': Web3.to_checksum_address(UNISWAP_V4_ROUTER),
        'data': calldata,
        'value': 0,
        'gas': 350_000,
        'maxFeePerGas': w3.eth.gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei(0.001, 'gwei'),
        'nonce': w3.eth.get_transaction_count(account.address),
        'chainId': 8453,
    }

    try:
        estimated = w3.eth.estimate_gas(tx)
        tx['gas'] = int(estimated * 1.3)
    except Exception as e:
        write_log(f'EXECUTOR | V4 gas estimate failed: {e}')
        # Try with different fee tiers
        for fee, tick in [(500, 10), (3000, 60), (10000, 200)]:
            try:
                calldata = _build_v4_swap_data_with_fee(
                    WETH_ADDRESS, token_address, account.address, amount_in_wei, 0, fee, tick)
                tx['data'] = calldata
                estimated = w3.eth.estimate_gas(tx)
                tx['gas'] = int(estimated * 1.3)
                write_log(f'EXECUTOR | V4 fee {fee} works, gas estimate {estimated}')
                break
            except Exception:
                continue
        else:
            return {'success': False, 'error': f'V4 gas estimate failed for all fee tiers: {e}'}

    tx_hash = sign_and_send(tx)
    receipt = wait_for_receipt(tx_hash, timeout=CONFIRMATION_TIMEOUT)

    if receipt.get('status') == 1:
        token_after = get_token_balance(token_address)
        token_received = token_after - token_before
        return {
            'success': True,
            'tx_hash': tx_hash,
            'token_amount': token_received,
            'eth_spent': float(w3.from_wei(amount_in_wei, 'ether')),
            'pool_version': 'v4',
        }
    else:
        return {'success': False, 'error': 'V4 transaction reverted', 'tx_hash': tx_hash}


def _sell_v4(w3: Web3, account, token_address: str,
             token_amount: int, slippage_pct: float) -> dict:
    """Execute a sell on Uniswap V4 (Token → WETH → ETH)."""
    weth_before = get_weth_balance()

    # Approve token for V4 router (already done in sell_token)
    # Execute V4 swap (Token → WETH)
    calldata = _build_v4_swap_data(
        token_in=token_address,
        token_out=WETH_ADDRESS,
        recipient=account.address,
        amount_in=token_amount,
        min_amount_out=0,
    )

    tx = {
        'from': account.address,
        'to': Web3.to_checksum_address(UNISWAP_V4_ROUTER),
        'data': calldata,
        'value': 0,
        'gas': 350_000,
        'maxFeePerGas': w3.eth.gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei(0.001, 'gwei'),
        'nonce': w3.eth.get_transaction_count(account.address),
        'chainId': 8453,
    }

    try:
        estimated = w3.eth.estimate_gas(tx)
        tx['gas'] = int(estimated * 1.3)
    except Exception as e:
        # Try different fee tiers
        for fee, tick in [(100, 1), (500, 10), (3000, 60), (10000, 200)]:
            try:
                calldata = _build_v4_swap_data_with_fee(
                    token_address, WETH_ADDRESS, account.address, token_amount, 0, fee, tick)
                tx['data'] = calldata
                estimated = w3.eth.estimate_gas(tx)
                tx['gas'] = int(estimated * 1.3)
                break
            except Exception:
                continue
        else:
            return {'success': False, 'error': f'V4 sell gas estimate failed: {e}'}

    tx_hash = sign_and_send(tx)
    receipt = wait_for_receipt(tx_hash, timeout=CONFIRMATION_TIMEOUT)

    if receipt.get('status') == 1:
        # Check WETH received, then unwrap
        weth_after = get_weth_balance()
        weth_received_wei = w3.to_wei(weth_after - weth_before, 'ether')

        if weth_received_wei > 0:
            try:
                _unwrap_weth(w3, account, weth_received_wei)
            except Exception as e:
                write_log(f'EXECUTOR | WETH unwrap failed (WETH still in wallet): {e}')

        eth_received = weth_after - weth_before
        return {
            'success': True,
            'tx_hash': tx_hash,
            'eth_received': max(eth_received, 0),
            'gas_cost': float(w3.from_wei(
                receipt.get('gasUsed', 0) * receipt.get('effectiveGasPrice', 0), 'ether')),
            'pool_version': 'v4',
        }
    else:
        return {'success': False, 'error': 'V4 sell transaction reverted', 'tx_hash': tx_hash}


def _build_v4_swap_data_with_fee(token_in: str, token_out: str, recipient: str,
                                  amount_in: int, min_out: int, fee: int, tick_spacing: int) -> bytes:
    """Build V4 swap calldata with specific fee tier and tick spacing."""
    step = (
        2,
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        '0x0000000000000000000000000000000000000000',
        fee,
        tick_spacing,
        '0x0000000000000000000000000000000000000000',
        b'',
    )
    encoded = encode(
        ['(uint8,address,address,address,uint24,int24,address,bytes)[]', 'address', 'uint256', 'uint256'],
        [[step], Web3.to_checksum_address(recipient), amount_in, min_out]
    )
    return V4_SWAP_SELECTOR + encoded


# === V3 Swap Implementation ===

def _buy_v3(w3: Web3, account, token_address: str,
            amount_in_wei: int, slippage_pct: float) -> dict:
    """Execute a buy on Uniswap V3 (ETH → Token)."""
    router = w3.eth.contract(
        address=Web3.to_checksum_address(UNISWAP_V3_ROUTER),
        abi=UNISWAP_V3_ABI,
    )

    token_before = get_token_balance(token_address)

    # Try fee tiers in order of likelihood for meme coins
    for fee in [3000, 10000, 500, 100]:
        params = (
            Web3.to_checksum_address(WETH_ADDRESS),
            Web3.to_checksum_address(token_address),
            fee,
            account.address,
            amount_in_wei,
            0,
            0,
        )

        tx = router.functions.exactInputSingle(params).build_transaction({
            'from': account.address,
            'value': amount_in_wei,
            'gas': 300_000,
            'maxFeePerGas': w3.eth.gas_price * 2,
            'maxPriorityFeePerGas': w3.to_wei(0.001, 'gwei'),
            'nonce': w3.eth.get_transaction_count(account.address),
            'chainId': 8453,
        })

        try:
            estimated = w3.eth.estimate_gas(tx)
            tx['gas'] = int(estimated * 1.2)

            tx_hash = sign_and_send(tx)
            receipt = wait_for_receipt(tx_hash, timeout=CONFIRMATION_TIMEOUT)

            if receipt.get('status') == 1:
                token_after = get_token_balance(token_address)
                return {
                    'success': True,
                    'tx_hash': tx_hash,
                    'token_amount': token_after - token_before,
                    'eth_spent': float(w3.from_wei(amount_in_wei, 'ether')),
                    'pool_version': 'v3',
                    'fee_tier': fee,
                }
            else:
                return {'success': False, 'error': f'V3 tx reverted (fee {fee})', 'tx_hash': tx_hash}
        except Exception as e:
            continue

    return {'success': False, 'error': 'V3 failed all fee tiers'}


def _sell_v3(w3: Web3, account, token_address: str,
             token_amount: int, slippage_pct: float) -> dict:
    """Execute a sell on Uniswap V3 (Token → ETH)."""
    router = w3.eth.contract(
        address=Web3.to_checksum_address(UNISWAP_V3_ROUTER),
        abi=UNISWAP_V3_ABI,
    )

    eth_before = w3.eth.get_balance(account.address)

    for fee in [3000, 10000, 500, 100]:
        params = (
            Web3.to_checksum_address(token_address),
            Web3.to_checksum_address(WETH_ADDRESS),
            fee,
            account.address,
            token_amount,
            0,
            0,
        )

        tx = router.functions.exactInputSingle(params).build_transaction({
            'from': account.address,
            'value': 0,
            'gas': 300_000,
            'maxFeePerGas': w3.eth.gas_price * 2,
            'maxPriorityFeePerGas': w3.to_wei(0.001, 'gwei'),
            'nonce': w3.eth.get_transaction_count(account.address),
            'chainId': 8453,
        })

        try:
            estimated = w3.eth.estimate_gas(tx)
            tx['gas'] = int(estimated * 1.2)

            tx_hash = sign_and_send(tx)
            receipt = wait_for_receipt(tx_hash, timeout=CONFIRMATION_TIMEOUT)

            if receipt.get('status') == 1:
                eth_after = w3.eth.get_balance(account.address)
                eth_received = float(w3.from_wei(eth_after - eth_before, 'ether'))
                gas_cost = float(w3.from_wei(
                    receipt.get('gasUsed', 0) * receipt.get('effectiveGasPrice', 0), 'ether'))
                eth_received += gas_cost

                return {
                    'success': True,
                    'tx_hash': tx_hash,
                    'eth_received': max(eth_received, 0),
                    'gas_cost': gas_cost,
                    'pool_version': 'v3',
                    'fee_tier': fee,
                }
            else:
                return {'success': False, 'error': f'V3 sell reverted (fee {fee})', 'tx_hash': tx_hash}
        except Exception:
            continue

    return {'success': False, 'error': 'V3 sell failed all fee tiers'}


# === Utility ===

def _ensure_approval(w3: Web3, account, token_address: str,
                     spender: str, amount: int) -> None:
    """Ensure the router has approval to spend our tokens."""
    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=ERC20_APPROVE_ABI,
    )

    current_allowance = token.functions.allowance(
        account.address,
        Web3.to_checksum_address(spender),
    ).call()

    if current_allowance >= amount:
        return

    max_uint = 2**256 - 1
    tx = token.functions.approve(
        Web3.to_checksum_address(spender),
        max_uint,
    ).build_transaction({
        'from': account.address,
        'gas': 100_000,
        'maxFeePerGas': w3.eth.gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei(0.001, 'gwei'),
        'nonce': w3.eth.get_transaction_count(account.address),
        'chainId': 8453,
    })

    tx_hash = sign_and_send(tx)
    wait_for_receipt(tx_hash, timeout=30)
    write_log(f'EXECUTOR | Approved {token_address[:12]}... for {spender[:12]}...')
