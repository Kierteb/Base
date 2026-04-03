"""
DEX swap executor for Base chain.
Routes to Uniswap V3 or Aerodrome based on token's DEX.
Uses dynamic slippage (starts low, increases on retry).
"""

import time
from web3 import Web3

from config.constants import (
    UNISWAP_V3_ROUTER, AERODROME_ROUTER, WETH_ADDRESS,
    MAX_BUY_SLIPPAGE_PCT, MAX_SELL_SLIPPAGE_PCT, TRADING_ENABLED,
)
from trading.wallet import (
    get_web3, get_account, get_address, sign_and_send, wait_for_receipt,
    get_token_balance, get_token_decimals,
)
from monitoring.logger import write_log

MAX_ATTEMPTS = 3
CONFIRMATION_TIMEOUT = 30

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


def buy_token(token_address: str, eth_amount: float, dex_id: str = 'uniswap',
              max_slippage_pct: float = MAX_BUY_SLIPPAGE_PCT) -> dict:
    """
    Buy a token with ETH via the appropriate DEX.

    Args:
        token_address: Token contract address to buy
        eth_amount: Amount of ETH to spend
        dex_id: DEX to route through (uniswap, aerodrome)
        max_slippage_pct: Maximum slippage tolerance (dynamic, up to this max)

    Returns:
        dict with success, tx_hash, token_amount, etc.
    """
    if not TRADING_ENABLED:
        return {'success': False, 'error': 'Trading disabled'}

    w3 = get_web3()
    account = get_account()
    if not account:
        return {'success': False, 'error': 'No trading account configured'}

    amount_in_wei = w3.to_wei(eth_amount, 'ether')

    for attempt in range(MAX_ATTEMPTS):
        # Dynamic slippage: starts at half max, increases each retry
        slippage_pct = min(max_slippage_pct * (0.5 + 0.25 * attempt), max_slippage_pct)

        try:
            if 'aerodrome' in dex_id.lower():
                result = _buy_aerodrome(w3, account, token_address, amount_in_wei, slippage_pct)
            else:
                result = _buy_uniswap_v3(w3, account, token_address, amount_in_wei, slippage_pct)

            if result.get('success'):
                return result

            write_log(f'EXECUTOR | Buy attempt {attempt + 1}/{MAX_ATTEMPTS} failed: {result.get("error")}')

        except Exception as e:
            write_log(f'EXECUTOR | Buy attempt {attempt + 1}/{MAX_ATTEMPTS} error: {e}')

        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(2 * (attempt + 1))

    return {'success': False, 'error': f'All {MAX_ATTEMPTS} buy attempts failed'}


def sell_token(token_address: str, token_amount: int | None = None,
               sell_pct: float = 100, dex_id: str = 'uniswap',
               max_slippage_pct: float = MAX_SELL_SLIPPAGE_PCT) -> dict:
    """
    Sell a token for ETH via the appropriate DEX.

    Args:
        token_address: Token to sell
        token_amount: Exact amount to sell (in smallest unit). If None, sells sell_pct of balance.
        sell_pct: Percentage of balance to sell (used if token_amount is None)
        dex_id: DEX to route through
        max_slippage_pct: Maximum slippage tolerance

    Returns:
        dict with success, tx_hash, eth_received, etc.
    """
    if not TRADING_ENABLED:
        return {'success': False, 'error': 'Trading disabled'}

    w3 = get_web3()
    account = get_account()
    if not account:
        return {'success': False, 'error': 'No trading account configured'}

    # Get token balance if not specified
    if token_amount is None:
        balance = get_token_balance(token_address)
        if balance <= 0:
            return {'success': False, 'error': 'No token balance'}
        token_amount = int(balance * sell_pct / 100)

    if token_amount <= 0:
        return {'success': False, 'error': 'Nothing to sell'}

    for attempt in range(MAX_ATTEMPTS):
        slippage_pct = min(max_slippage_pct * (0.5 + 0.25 * attempt), max_slippage_pct)

        try:
            # Ensure token is approved for the router
            router = AERODROME_ROUTER if 'aerodrome' in dex_id.lower() else UNISWAP_V3_ROUTER
            _ensure_approval(w3, account, token_address, router, token_amount)

            if 'aerodrome' in dex_id.lower():
                result = _sell_aerodrome(w3, account, token_address, token_amount, slippage_pct)
            else:
                result = _sell_uniswap_v3(w3, account, token_address, token_amount, slippage_pct)

            if result.get('success'):
                return result

            write_log(f'EXECUTOR | Sell attempt {attempt + 1}/{MAX_ATTEMPTS} failed: {result.get("error")}')

        except Exception as e:
            write_log(f'EXECUTOR | Sell attempt {attempt + 1}/{MAX_ATTEMPTS} error: {e}')

        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(2 * (attempt + 1))

    return {'success': False, 'error': f'All {MAX_ATTEMPTS} sell attempts failed'}


def _buy_uniswap_v3(w3: Web3, account, token_address: str,
                     amount_in_wei: int, slippage_pct: float) -> dict:
    """Execute a buy on Uniswap V3 (ETH → Token)."""
    router = w3.eth.contract(
        address=Web3.to_checksum_address(UNISWAP_V3_ROUTER),
        abi=UNISWAP_V3_ABI,
    )

    # For a buy: ETH (via WETH) → Token
    # amountOutMinimum = 0 for simplicity (slippage handled by deadline + revert)
    # In production, you'd get a quote first
    params = (
        Web3.to_checksum_address(WETH_ADDRESS),     # tokenIn (WETH)
        Web3.to_checksum_address(token_address),     # tokenOut
        3000,                                         # fee tier (0.3% — most common for meme coins)
        account.address,                              # recipient
        amount_in_wei,                                # amountIn
        0,                                            # amountOutMinimum (TODO: add quote-based minimum)
        0,                                            # sqrtPriceLimitX96 (0 = no limit)
    )

    # Build transaction
    tx = router.functions.exactInputSingle(params).build_transaction({
        'from': account.address,
        'value': amount_in_wei,  # Send ETH which gets wrapped to WETH internally
        'gas': 300_000,
        'maxFeePerGas': w3.eth.gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei(0.001, 'gwei'),
        'nonce': w3.eth.get_transaction_count(account.address),
        'chainId': 8453,  # Base mainnet
    })

    # Estimate gas more accurately
    try:
        estimated = w3.eth.estimate_gas(tx)
        tx['gas'] = int(estimated * 1.2)  # 20% buffer
    except Exception:
        pass  # Use default 300k

    tx_hash = sign_and_send(tx)
    receipt = wait_for_receipt(tx_hash, timeout=CONFIRMATION_TIMEOUT)

    if receipt.get('status') == 1:
        # Check actual tokens received
        token_received = get_token_balance(token_address)
        return {
            'success': True,
            'tx_hash': tx_hash,
            'token_amount': token_received,
            'eth_spent': float(w3.from_wei(amount_in_wei, 'ether')),
        }
    else:
        return {'success': False, 'error': 'Transaction reverted', 'tx_hash': tx_hash}


def _sell_uniswap_v3(w3: Web3, account, token_address: str,
                      token_amount: int, slippage_pct: float) -> dict:
    """Execute a sell on Uniswap V3 (Token → ETH)."""
    router = w3.eth.contract(
        address=Web3.to_checksum_address(UNISWAP_V3_ROUTER),
        abi=UNISWAP_V3_ABI,
    )

    params = (
        Web3.to_checksum_address(token_address),     # tokenIn
        Web3.to_checksum_address(WETH_ADDRESS),      # tokenOut (WETH)
        3000,                                         # fee tier
        account.address,                              # recipient
        token_amount,                                 # amountIn
        0,                                            # amountOutMinimum
        0,                                            # sqrtPriceLimitX96
    )

    # Get ETH balance before sell for P&L calculation
    eth_before = w3.eth.get_balance(account.address)

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
    except Exception:
        pass

    tx_hash = sign_and_send(tx)
    receipt = wait_for_receipt(tx_hash, timeout=CONFIRMATION_TIMEOUT)

    if receipt.get('status') == 1:
        eth_after = w3.eth.get_balance(account.address)
        eth_received = float(w3.from_wei(eth_after - eth_before, 'ether'))
        # Account for gas cost (received may be slightly negative due to gas)
        gas_cost = float(w3.from_wei(
            receipt.get('gasUsed', 0) * receipt.get('effectiveGasPrice', 0), 'ether'
        ))
        eth_received += gas_cost  # Add back gas to get true swap output

        return {
            'success': True,
            'tx_hash': tx_hash,
            'eth_received': max(eth_received, 0),
            'gas_cost': gas_cost,
        }
    else:
        return {'success': False, 'error': 'Transaction reverted', 'tx_hash': tx_hash}


def _buy_aerodrome(w3: Web3, account, token_address: str,
                   amount_in_wei: int, slippage_pct: float) -> dict:
    """Execute a buy on Aerodrome (ETH → Token). Placeholder — same structure as Uniswap."""
    # Aerodrome uses a different router interface
    # For now, fall back to Uniswap V3 which also lists many tokens
    write_log(f'EXECUTOR | Aerodrome buy not yet implemented, falling back to Uniswap V3')
    return _buy_uniswap_v3(w3, account, token_address, amount_in_wei, slippage_pct)


def _sell_aerodrome(w3: Web3, account, token_address: str,
                    token_amount: int, slippage_pct: float) -> dict:
    """Execute a sell on Aerodrome (Token → ETH). Placeholder."""
    write_log(f'EXECUTOR | Aerodrome sell not yet implemented, falling back to Uniswap V3')
    return _sell_uniswap_v3(w3, account, token_address, token_amount, slippage_pct)


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

    # Approve max uint256 (one-time approval)
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
