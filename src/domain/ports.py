from abc import ABC, abstractmethod
from typing import Optional

from domain.entities import Captura, ClassificacaoResultado


class IStorageService(ABC):
    @abstractmethod
    def upload_image(self, key: str, image_bytes: bytes) -> None:
        ...

    @abstractmethod
    def delete_image(self, key: str) -> None:
        ...

    @abstractmethod
    def download_image(self, key: str) -> bytes:
        ...

    @abstractmethod
    def generate_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        ...


class ICapturaRepository(ABC):
    @abstractmethod
    def save(self, captura: Captura) -> None:
        ...

    @abstractmethod
    def delete(self, plantacao_id: str, timestamp: str, captura_id: str) -> None:
        ...

    @abstractmethod
    def get(self, plantacao_id: str, timestamp: str, captura_id: str) -> Captura | None:
        ...

    @abstractmethod
    def update(self, captura: Captura) -> None:
        ...

    @abstractmethod
    def list_by_status(
        self, status: str, plantacao_id: str = "plantacao-mock-001", limit: int = 50
    ) -> list[Captura]:
        ...

    @abstractmethod
    def list_by_plantacao(
        self,
        plantacao_id: str,
        status: Optional[str] = None,
        status_geral: Optional[str] = None,
        origem: Optional[str] = None,
        data_inicio: Optional[str] = None,
        data_fim: Optional[str] = None,
        pagina: int = 1,
        tamanho_pagina: int = 8,
        fl_treino: Optional[int] = None,
    ) -> tuple[list[Captura], int]:
        """Retorna (capturas_da_pagina, total_de_capturas_no_filtro), mais
        recentes primeiro por padrao. fl_treino=0 devolve so as que NAO
        foram usadas em treino/validacao (inclui itens sem a coluna);
        fl_treino=1 so as usadas; None nao filtra (usado pelas rotinas de
        manutencao, que precisam percorrer tudo)."""
        ...

    @abstractmethod
    def list_pontos_mapa(
        self,
        plantacao_id: str,
        status_geral: Optional[str] = None,
        fl_treino: Optional[int] = None,
    ) -> list[dict]:
        """Pontos leves pro mapa (captura_id, timestamp, latitude, longitude,
        status_geral), mais recentes primeiro, sem carregar o item inteiro."""
        ...

    @abstractmethod
    def list_cliente_ids(self) -> list[str]:
        ...


class IClassificationService(ABC):
    @abstractmethod
    def classify(self, image_bytes: bytes) -> ClassificacaoResultado:
        ...


class IAuthService(ABC):
    @abstractmethod
    def verify_token(self, token: str) -> str:
        """Valida JWT e retorna cliente_id (sub)."""
        ...

    @abstractmethod
    def cadastrar(self, email: str, senha: str) -> None:
        ...

    @abstractmethod
    def login(self, email: str, senha: str) -> dict:
        """Retorna dict com access_token, refresh_token, id_token."""
        ...

    @abstractmethod
    def recuperar_senha(self, email: str) -> None:
        ...

    @abstractmethod
    def confirmar_cadastro(self, email: str, codigo: str) -> None:
        ...


class IEmailService(ABC):
    """Envio de e-mail — usado hoje so pro aviso de 'foto nao e milho' no
    upload manual, mas desenhado generico o suficiente pra outros usos
    futuros (recuperacao de senha customizada, relatorio semanal, etc.)."""

    @abstractmethod
    def enviar_email(
        self,
        destinatario: str,
        assunto: str,
        corpo_texto: str,
        corpo_html: str | None = None,
    ) -> None:
        ...


class IUserLookupService(ABC):
    """Busca dados de um usuario (hoje so o email) a partir do cliente_id.

    Isolado do servico de autenticacao de proposito — nao mexe no fluxo
    de login/cadastro que ja existe, so adiciona essa consulta extra."""

    @abstractmethod
    def obter_email(self, cliente_id: str) -> str | None:
        ...
