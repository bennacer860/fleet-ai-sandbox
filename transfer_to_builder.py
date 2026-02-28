import os
import sys
import time
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

def main():
    load_dotenv()
    
    # Configuration
    private_key = os.getenv("PRIVATE_KEY")
    # THE OLD PROXY ADDRESS (as seen in your logs)
    old_proxy = "0xd015de9ba4b79a2e439d59f90c8236dbee940649"
    # THE NEW BUILDER SAFE
    new_safe = "0x418D41dBe8bCBd5f926DE0DC81138cd2a695cA32"
    
    clob_host = "https://clob.polymarket.com"
    
    print(f"--- Polymarket Fund Transfer ---")
    print(f"Source (Old Proxy): {old_proxy}")
    print(f"Destination (Safe): {new_safe}")
    
    # Initialize client for the OLD wallet first
    client = ClobClient(
        clob_host,
        key=private_key,
        chain_id=137,
        signature_type=1, # Old wallet was email/proxy type
        funder=old_proxy
    )
    
    try:
        # Get credentials
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        
        # 1. Check Balance
        # We use a simple direct check for common ERC20 on Polygon
        import httpx
        usdc_poly = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        r = httpx.get(f"https://polygon.blockscout.com/api/v2/addresses/{old_proxy}/tokens?type=ERC-20")
        balance = 0
        for item in r.json().get("items", []):
            if item.get("token", {}).get("symbol") == "USDC":
                balance = int(item.get("value")) / 1e6
                break
        
        if balance <= 0:
            print(f"ERROR: No USDC found in old wallet {old_proxy}")
            return

        print(f"Found {balance:.2f} USDC in old wallet.")
        
        confirm = input(f"Transfer {balance:.2f} USDC to {new_safe}? (y/n): ")
        if confirm.lower() != 'y':
            print("Cancelled.")
            return

        # 2. Execute Withdrawal/Transfer
        # In Polymarket, 'withdraw' from a proxy to another address is a transfer
        print("Initiating transfer...")
        # We leave a tiny bit for safety, or send all
        resp = client.post_withdrawal({
            "amount": balance,
            "token_id": "0", # USDC index
            "recipient": new_safe
        })
        
        if resp.get("success"):
            print(f"SUCCESS! Transfer submitted.")
            print(f"Transaction ID: {resp.get('transactionId', 'N/A')}")
        else:
            print(f"FAILED: {resp.get('errorMsg')}")
            
    except Exception as e:
        print(f"CRITICAL ERROR: {e}")

if __name__ == "__main__":
    main()
