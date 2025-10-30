from typing import Any

from shared.domain.ports.secrets_provider import ISecretsProvider


class AwsSecretsAdapter(ISecretsProvider):
    def __init__(self, client: Any, key_name: str):
        self.client = client
        self.key_name = key_name

    def get_secret(self) -> str | None:
        secret = self.client.get_secret_value(SecretId=self.key_name)
        if secret.SecretString is None:
            return None
        return secret.SecretString
