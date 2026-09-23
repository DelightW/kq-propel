"""
Sends one real Safaricom STK push, for manual verification.

Why this is a tool and not a chat command: the assistant only ever charges an
amount it found in a policy section. A one-shilling test amount comes from
nowhere in the corpus, so exposing it through the agent would punch a hole in
exactly the grounding rule the project is built on. Verification belongs in a
tool the operator runs deliberately.

    python tools/send_test_stk.py --phone 0712345678 --amount 1

Without DARAJA_* credentials this refuses to run rather than printing a
simulated success, because a simulated success proves nothing about Safaricom.
"""
import argparse
import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config, daraja  # noqa: E402


def normalize(phone: str) -> str:
    """Safaricom wants 2547XXXXXXXX / 2541XXXXXXXX with no plus sign."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    elif digits.startswith("7") or digits.startswith("1"):
        digits = "254" + digits
    return digits


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one real STK push.")
    parser.add_argument("--phone", required=True, help="Safaricom number, e.g. 0712345678")
    parser.add_argument("--amount", type=int, default=1, help="Whole shillings (default 1)")
    parser.add_argument("--reference", default="KQPROPEL-TEST")
    parser.add_argument("--description", default="KQ-Propel STK verification")
    args = parser.parse_args()

    posture = daraja.describe()
    print("=" * 70)
    print("KQ-Propel STK push verification")
    print("=" * 70)
    for key, value in posture.items():
        print(f"  {key:<24}: {value}")

    if not posture["configured"]:
        print("\nREFUSING TO SEND.")
        print("  DARAJA_CONSUMER_KEY, DARAJA_CONSUMER_SECRET and DARAJA_PASSKEY")
        print("  are not all set in .env, so no request would reach Safaricom.")
        print("\n  Get sandbox credentials at https://developer.safaricom.co.ke:")
        print("    1. Create an app; copy its Consumer Key and Consumer Secret.")
        print("    2. Open 'M-Pesa Express' (Lipa Na M-Pesa Online) for the test")
        print("       shortcode 174379 and copy the Passkey.")
        print("    3. Put all three in .env and run this again.")
        print("\n  A sandbox push reaches a real handset. No money moves.")
        return 2

    phone = normalize(args.phone)
    if not re.fullmatch(r"254[17]\d{8}", phone):
        print(f"\n{phone} is not a Kenyan mobile number Safaricom will accept.")
        return 2

    if not posture["environment"] == "sandbox":
        print("\nWARNING: DARAJA_SANDBOX=false, so this is PRODUCTION.")
        print(f"         Ksh {args.amount} will actually be debited.")
        if input("         Type 'yes' to continue: ").strip().lower() != "yes":
            print("         Aborted.")
            return 1

    print(f"\nSending Ksh {args.amount} prompt to {phone} ...")
    result = daraja.initiate_stk_push(
        phone_number=phone, amount=args.amount,
        reference=args.reference, description=args.description)
    print(json.dumps(result, indent=2))

    if result.get("success"):
        print("\nAccepted by Safaricom. Check the handset for the PIN prompt.")
        if posture["callback_is_placeholder"]:
            print("Note: DARAJA_CALLBACK_URL is a placeholder, so no result")
            print("callback can arrive. The prompt appearing on the phone is the")
            print("evidence here; the final status will stay unconfirmed.")
    else:
        print("\nRejected. The reason from Safaricom is in 'error' above.")
        print("Common causes: wrong passkey for the shortcode, shortcode/passkey")
        print("mismatch, or sandbox credentials used against the production host.")
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
