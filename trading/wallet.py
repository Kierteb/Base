"""
ETH wallet management for Base chain via web3.py.
Handles account setup, balance checks, and transaction signing.
"""

import os
from web3 import Web3
from eth_account import Account

from config.constants import BASE_RPC_URL, BASE_PRIVATE_KEY, WETH_ADDRESS
from monitoring.logger import write_log

# Lazy-initialised web3 instance
_w3: Web3 | None = None
_account = None


def get_web3() -> Web3:
    """Get or create the Web3 instance connected to Base."""
    global _w3
    if _w3 is None:
        _w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL, request_kwargs={'timeout': 30}))
        if not _w3.is_connected():
            write_log('WALLET | WARNING: Web3 not connected to Base RPC')
    return _w3


def get_account():
    """Get the trading account from private key."""
    global _account
    if _account is None and BASE_PRIVATE_KEY:
        _account = Account.from_key(BASE_PRIVATE_KEY)
    return _account


def get_address() -> str:
    """Get the wallet address."""
    account = get_account()
    return account.address if account else ''


def get_eth_balance() -> float:
    """Get ETH balance of the trading wallet."""
    w3 = get_web3()
    account = get_account()
    if not account:
        return 0.0
    try:
        balance_wei = w3.eth.get_balance(account.address)
        return float(w3.from_wei(balance_wei, 'ether'))
    except Exception as e:
        write_log(f'WALLET | Balance check error: {e}')
        return 0.0


def get_weth_balance() -> float:
    """Get WETH balance of the trading wallet."""
    w3 = get_web3()
    account = get_account()
    if not account:
        return 0.0

    # ERC20 balanceOf ABI
    abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
            "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
            "type": "function"}]

    try:
        weth = w3.eth.contract(address=Web3.to_checksum_address(WETH_ADDRESS), abi=abi)
        balance = weth.functions.balanceOf(account.address).call()
        return float(w3.from_wei(balance, 'ether'))
    except Exception as e:
        write_log(f'WALLET | WETH balance check error: {e}')
        return 0.0


def get_token_balance(token_address: str) -> int:
    """Get raw token balance (in smallest unit) for the trading wallet."""
    w3 = get_web3()
    account = get_account()
    if not account:
        return 0

    abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
            "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
            "type": "function"}]

    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=abi)
        return token.functions.balanceOf(account.address).call()
    except Exception as e:
        write_log(f'WALLET | Token balance check error for {token_address[:12]}: {e}')
        return 0


def get_token_decimals(token_address: str) -> int:
    """Get token decimals."""
    w3 = get_web3()
    abi = [{"constant": True, "inputs": [], "name": "decimals",
            "outputs": [{"name": "", "type": "uint8"}], "type": "function"}]
    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=abi)
        return token.functions.decimals().call()
    except Exception:
        return 18


def sign_and_send(tx: dict) -> str:
    """Sign and send a transaction. Returns tx hash."""
    w3 = get_web3()
    account = get_account()
    if not account:
        raise ValueError('No trading account configured')

    signed = w3.eth.account.sign_transaction(tx, account.key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def wait_for_receipt(tx_hash: str, timeout: int = 30) -> dict:
    """Wait for transaction confirmation."""
    w3 = get_web3()
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    return dict(receipt)


if __name__ == '__main__':
    w3 = get_web3()
    print(f'  Connected to Base: {w3.is_connected()}')
    print(f'  Chain ID: {w3.eth.chain_id}')

    account = get_account()
    if account:
        print(f'  Address: {account.address}')
        print(f'  ETH balance: {get_eth_balance():.6f}')
        print(f'  WETH balance: {get_weth_balance():.6f}')
    else:
        print('  No private key configured')
