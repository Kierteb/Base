"""
Contract analysis via Basescan API.
Checks: verification status, proxy detection, ownership, deployer history.
"""

import requests

from config.constants import BASESCAN_API_KEY, BASESCAN_BASE_URL, BURN_ADDRESSES
from monitoring.logger import write_log


def check_verified(contract_address: str) -> dict:
    """
    Check if contract source code is verified on Basescan.
    Verified source = we can inspect the transfer function for hidden logic.
    """
    try:
        resp = requests.get(BASESCAN_BASE_URL, params={
            'module': 'contract',
            'action': 'getabi',
            'address': contract_address,
            'apikey': BASESCAN_API_KEY,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        write_log(f'BASESCAN | Verification check error for {contract_address[:12]}: {e}')
        return {'verified': False, 'error': str(e)}

    if data.get('status') == '1' and data.get('result') not in ('', 'Contract source code not verified'):
        return {'verified': True, 'abi': data['result']}
    return {'verified': False, 'message': data.get('result', 'Unknown')}


def get_contract_creation(contract_address: str) -> dict:
    """Get contract creator address and creation tx hash."""
    try:
        resp = requests.get(BASESCAN_BASE_URL, params={
            'module': 'contract',
            'action': 'getcontractcreation',
            'contractaddresses': contract_address,
            'apikey': BASESCAN_API_KEY,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        write_log(f'BASESCAN | Creation lookup error: {e}')
        return {'deployer': '', 'tx_hash': '', 'error': str(e)}

    results = data.get('result') or []
    if results and isinstance(results, list) and len(results) > 0:
        return {
            'deployer': results[0].get('contractCreator', ''),
            'tx_hash': results[0].get('txHash', ''),
        }
    return {'deployer': '', 'tx_hash': ''}


def count_deployer_contracts(deployer: str) -> int:
    """
    Count how many contracts a deployer has created.
    Serial deployers (5+) are likely scam factories.
    """
    if not deployer:
        return 0

    try:
        # Get internal transactions from deployer (contract creations appear as internal txns)
        resp = requests.get(BASESCAN_BASE_URL, params={
            'module': 'account',
            'action': 'txlist',
            'address': deployer,
            'startblock': 0,
            'endblock': 99999999,
            'page': 1,
            'offset': 100,  # Check last 100 txns
            'sort': 'desc',
            'apikey': BASESCAN_API_KEY,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        write_log(f'BASESCAN | Deployer contract count error: {e}')
        return 0

    txns = data.get('result') or []
    if not isinstance(txns, list):
        return 0

    # Count contract creation transactions (to address is empty for contract deployments)
    contract_creations = sum(1 for tx in txns if tx.get('to', '') == '')
    return contract_creations


def check_ownership(goplus_data: dict) -> dict:
    """
    Analyse ownership status from GoPlus data.
    Returns ownership assessment.
    """
    owner = goplus_data.get('owner_address', '')
    is_renounced = owner.lower() in BURN_ADDRESSES or owner == '' if owner else False
    can_take_back = goplus_data.get('can_take_back_ownership', False)
    hidden_owner = goplus_data.get('hidden_owner', False)
    owner_can_change_balance = goplus_data.get('owner_change_balance', False)

    reasons = []
    if can_take_back:
        reasons.append('Owner can take back ownership')
    if hidden_owner:
        reasons.append('Hidden owner detected')
    if owner_can_change_balance:
        reasons.append('Owner can change balances')

    return {
        'owner_address': owner,
        'renounced': is_renounced,
        'can_take_back': can_take_back,
        'hidden_owner': hidden_owner,
        'owner_can_change_balance': owner_can_change_balance,
        'safe': is_renounced and not can_take_back and not hidden_owner,
        'reasons': reasons,
    }


def check_proxy(goplus_data: dict) -> dict:
    """
    Check if the token is a proxy/upgradeable contract.
    Proxy contracts can have their logic changed post-deployment.
    """
    is_proxy = goplus_data.get('is_proxy', False)
    return {
        'is_proxy': is_proxy,
        'safe': not is_proxy,
        'reason': 'Upgradeable proxy contract — logic can be changed' if is_proxy else '',
    }


def check_mintable(goplus_data: dict) -> dict:
    """Check if the owner can mint unlimited tokens."""
    is_mintable = goplus_data.get('is_mintable', False)
    return {
        'is_mintable': is_mintable,
        'safe': not is_mintable,
        'reason': 'Owner can mint unlimited tokens' if is_mintable else '',
    }


def check_blacklist_capability(goplus_data: dict) -> dict:
    """Check if the contract has a blacklist function."""
    is_blacklisted = goplus_data.get('is_blacklisted', False)
    owner_renounced = goplus_data.get('owner_address', '').lower() in BURN_ADDRESSES

    # Blacklist is only dangerous if owner is still active
    dangerous = is_blacklisted and not owner_renounced

    return {
        'has_blacklist': is_blacklisted,
        'dangerous': dangerous,
        'safe': not dangerous,
        'reason': 'Owner can blacklist addresses from selling' if dangerous else '',
    }


def run_contract_analysis(contract_address: str, goplus_data: dict) -> dict:
    """
    Run full contract analysis. Requires GoPlus data from honeypot.py.

    Returns:
        dict with keys:
            passed: bool
            verified: bool
            is_proxy: bool
            is_mintable: bool
            ownership_renounced: bool
            has_blacklist: bool
            deployer: str
            deployer_contract_count: int
            reasons: list[str] — failure reasons
            penalties: list[tuple[str, int]] — soft penalty signals
    """
    reasons = []
    penalties = []

    # 1. Contract verification (Basescan)
    verification = check_verified(contract_address)
    if not verification.get('verified'):
        reasons.append('Contract source code not verified on Basescan')

    # 2. Contract creation / deployer
    creation = get_contract_creation(contract_address)
    deployer = creation.get('deployer', '')
    deployer_count = count_deployer_contracts(deployer) if deployer else 0
    if deployer_count >= 5:
        penalties.append(('serial_deployer', -30))

    # 3. Proxy detection
    proxy = check_proxy(goplus_data)
    if not proxy['safe']:
        reasons.append(proxy['reason'])

    # 4. Mint function
    mint = check_mintable(goplus_data)
    if not mint['safe']:
        reasons.append(mint['reason'])

    # 5. Ownership
    ownership = check_ownership(goplus_data)
    if ownership.get('can_take_back'):
        reasons.append('Owner can take back ownership')
    if not ownership.get('renounced'):
        penalties.append(('owner_active', -20))
    if ownership.get('hidden_owner'):
        reasons.append('Hidden owner detected')
    if ownership.get('owner_can_change_balance'):
        reasons.append('Owner can change balances')

    # 6. Blacklist capability
    blacklist = check_blacklist_capability(goplus_data)
    if not blacklist['safe']:
        reasons.append(blacklist['reason'])

    # 7. External calls
    if goplus_data.get('external_call'):
        penalties.append(('external_calls', -25))

    # 8. Self-destruct
    if goplus_data.get('selfdestruct'):
        reasons.append('Contract has selfdestruct capability')

    # 9. Anti-whale modifiable
    if goplus_data.get('anti_whale_modifiable'):
        penalties.append(('modifiable_restrictions', -20))

    # 10. Transfer pausable (soft penalty — some legitimate tokens have this)
    if goplus_data.get('transfer_pausable') and not ownership.get('renounced'):
        penalties.append(('transfer_pausable', -10))

    # Hard reject if any reason exists
    passed = len(reasons) == 0

    return {
        'passed': passed,
        'verified': verification.get('verified', False),
        'is_proxy': proxy.get('is_proxy', False),
        'is_mintable': mint.get('is_mintable', False),
        'ownership_renounced': ownership.get('renounced', False),
        'has_blacklist': blacklist.get('has_blacklist', False),
        'deployer': deployer,
        'deployer_contract_count': deployer_count,
        'reasons': reasons,
        'penalties': penalties,
    }
