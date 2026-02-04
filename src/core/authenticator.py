"""RSA-PSS signature generation for Kalshi API authentication."""

import base64
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from config.logging_config import get_logger

logger = get_logger(__name__)


class KalshiAuthenticator:
    """Generates RSA-PSS signatures for Kalshi API requests.

    Kalshi uses RSA-PSS signatures for API authentication. Each request must include:
    - KALSHI-ACCESS-KEY: Your API key ID
    - KALSHI-ACCESS-TIMESTAMP: Unix timestamp in milliseconds
    - KALSHI-ACCESS-SIGNATURE: Base64-encoded RSA-PSS signature
    """

    def __init__(self, api_key_id: str, private_key_path: Path) -> None:
        """Initialize the authenticator.

        Args:
            api_key_id: Kalshi API key ID
            private_key_path: Path to RSA private key PEM file
        """
        self.api_key_id = api_key_id
        self._private_key = self._load_private_key(private_key_path)
        logger.info("Authenticator initialized", api_key_id=api_key_id)

    def _load_private_key(self, key_path: Path) -> rsa.RSAPrivateKey:
        """Load RSA private key from PEM file.

        Args:
            key_path: Path to PEM file

        Returns:
            RSA private key object
        """
        with open(key_path, "rb") as f:
            key_data = f.read()

        private_key = serialization.load_pem_private_key(key_data, password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("Key must be an RSA private key")

        return private_key

    def _get_timestamp(self) -> int:
        """Get current timestamp in milliseconds."""
        return int(time.time() * 1000)

    def _create_signature_payload(
        self,
        timestamp: int,
        method: str,
        path: str,
    ) -> str:
        """Create the message to sign.

        CRITICAL: The path must NOT include query parameters.
        Query params are stripped before signing.

        Args:
            timestamp: Unix timestamp in milliseconds
            method: HTTP method (GET, POST, etc.)
            path: Request path WITHOUT query parameters

        Returns:
            String to sign
        """
        # Strip query parameters - critical for signature validation
        parsed = urlparse(path)
        clean_path = parsed.path

        return f"{timestamp}{method.upper()}{clean_path}"

    def _sign(self, message: str) -> str:
        """Sign a message using RSA-PSS.

        Args:
            message: Message to sign

        Returns:
            Base64-encoded signature
        """
        signature = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def get_auth_headers(
        self,
        method: str,
        path: str,
        timestamp: Optional[int] = None,
    ) -> dict[str, str]:
        """Generate authentication headers for a request.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: Request path (query params will be stripped for signing)
            timestamp: Optional timestamp override (for testing)

        Returns:
            Dictionary of authentication headers
        """
        ts = timestamp or self._get_timestamp()
        payload = self._create_signature_payload(ts, method, path)
        signature = self._sign(payload)

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

        logger.debug(
            "Generated auth headers",
            method=method,
            path=path,
            timestamp=ts,
        )

        return headers
