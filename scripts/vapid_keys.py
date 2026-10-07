"""Print a VAPID key pair for Web Push. Put both in Render; only the public key ever reaches the browser.
Run: python scripts/vapid_keys.py"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

key = ec.generate_private_key(ec.SECP256R1())
raw_priv = key.private_numbers().private_value.to_bytes(32, "big")
raw_pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
print(f"VAPID_PUBLIC_KEY={b64(raw_pub)}")
print(f"VAPID_PRIVATE_KEY={b64(raw_priv)}")
