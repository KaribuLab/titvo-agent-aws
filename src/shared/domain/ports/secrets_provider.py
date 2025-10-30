import abc


class ISecretsProvider(abc.ABC):
    @abc.abstractmethod
    def get_secret(self, key: str) -> str | None:
        pass
