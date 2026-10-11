import base64
import uuid
from datetime import datetime

from application.preprocessamento_imagem import (
    extrair_timestamp_exif,
    gerar_thumbnail,
    redimensionar_para_classificacao,
)
from application.dtos import (
    CapturaDetalheDTO,
    CapturaInputDTO,
    CapturaOutputDTO,
    CapturaResumoDTO,
    CapturaSimplesInputDTO,
    ListCapturasOutputDTO,
    ListClientesOutputDTO,
)
from domain.entities import (
    Captura,
    Coordenadas,
    JetsonNanoInfo,
    StatusEntry,
)
from domain.ports import (
    ICapturaRepository,
    IClassificationService,
    IEmailService,
    IStorageService,
    IUserLookupService,
)

MOCK_PLANTACAO_ID = "plantacao-mock-001"
MOCK_CARRINHO_ID = "carrinho-mock-001"


class SubmitCapturaUseCase:
    def __init__(
        self,
        storage: IStorageService,
        repository: ICapturaRepository,
        s3_bucket: str,
    ):
        self._storage = storage
        self._repository = repository
        self._s3_bucket = s3_bucket

    def execute(self, dto: CapturaInputDTO) -> CapturaOutputDTO:
        image_bytes = base64.b64decode(dto.imagem_base64)

        now = datetime.utcnow()
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        short_uuid = uuid.uuid4().hex[:8]
        captura_id = f"{now.strftime('%Y%m%d%H%M%S')}-{short_uuid}"

        date = datetime.strptime(dto.dia_mes_ano, "%d/%m/%Y")
        dia, mes, ano = date.strftime("%d"), date.strftime("%m"), date.strftime("%Y")
        s3_key = f"{dto.cliente_id}/{ano}/{mes}/{dia}/{captura_id}.jpg"

        self._storage.upload_image(s3_key, image_bytes)

        thumbnail_key = None
        thumbnail_bytes = gerar_thumbnail(image_bytes)
        if thumbnail_bytes is not None:
            thumbnail_key = f"{dto.cliente_id}/{ano}/{mes}/{dia}/{captura_id}_thumb.jpg"
            try:
                self._storage.upload_image(thumbnail_key, thumbnail_bytes)
            except Exception:
                thumbnail_key = None

        captura = Captura(
            captura_id=captura_id,
            cliente_id=dto.cliente_id,
            plantacao_id=MOCK_PLANTACAO_ID,
            carrinho_id=MOCK_CARRINHO_ID,
            timestamp=timestamp,
            coordenadas=Coordenadas(
                latitude=dto.latitude,
                longitude=dto.longitude,
            ),
            s3_bucket=self._s3_bucket,
            s3_key=s3_key,
            thumbnail_key=thumbnail_key,
            jetson_nano=JetsonNanoInfo(
                planta_detectada=True,
                confianca=dto.confianca_borda if dto.confianca_borda is not None else 0.0,
                modelo_versao=dto.modelo_versao_borda or "desconhecida",
            ),
            status="PENDENTE",
            status_history=[StatusEntry(status="PENDENTE", timestamp=timestamp)],
            origem="rover",
        )

        self._repository.save(captura)

        return CapturaOutputDTO(
            sucesso=True,
            captura_id=captura_id,
            s3_key=s3_key,
            timestamp=timestamp,
            plantacao_id=MOCK_PLANTACAO_ID,
        )


class SubmitCapturaSimplesUseCase:
    """
    Upload manual simplificado — recebe SO a foto, sem data nem
    coordenadas. O timestamp vem do EXIF da propria imagem.
    """

    def __init__(
        self,
        storage: IStorageService,
        repository: ICapturaRepository,
        s3_bucket: str,
    ):
        self._storage = storage
        self._repository = repository
        self._s3_bucket = s3_bucket

    def execute(self, dto: CapturaSimplesInputDTO) -> CapturaOutputDTO:
        image_bytes = base64.b64decode(dto.imagem_base64)

        timestamp_exif = extrair_timestamp_exif(image_bytes)
        if timestamp_exif is not None:
            momento = datetime.strptime(timestamp_exif, "%Y-%m-%dT%H:%M:%SZ")
        else:
            momento = datetime.utcnow()
        timestamp = momento.strftime("%Y-%m-%dT%H:%M:%SZ")

        short_uuid = uuid.uuid4().hex[:8]
        captura_id = f"{momento.strftime('%Y%m%d%H%M%S')}-{short_uuid}"
        dia, mes, ano = momento.strftime("%d"), momento.strftime("%m"), momento.strftime("%Y")
        s3_key = f"{dto.cliente_id}/{ano}/{mes}/{dia}/{captura_id}.jpg"

        self._storage.upload_image(s3_key, image_bytes)

        thumbnail_key = None
        thumbnail_bytes = gerar_thumbnail(image_bytes)
        if thumbnail_bytes is not None:
            thumbnail_key = f"{dto.cliente_id}/{ano}/{mes}/{dia}/{captura_id}_thumb.jpg"
            try:
                self._storage.upload_image(thumbnail_key, thumbnail_bytes)
            except Exception:
                thumbnail_key = None

        captura = Captura(
            captura_id=captura_id,
            cliente_id=dto.cliente_id,
            plantacao_id=MOCK_PLANTACAO_ID,
            carrinho_id=MOCK_CARRINHO_ID,
            timestamp=timestamp,
            coordenadas=Coordenadas(latitude=None, longitude=None),
            s3_bucket=self._s3_bucket,
            s3_key=s3_key,
            thumbnail_key=thumbnail_key,
            jetson_nano=JetsonNanoInfo(
                planta_detectada=True,
                confianca=0.0,
                modelo_versao="upload-manual",
            ),
            status="PENDENTE",
            status_history=[StatusEntry(status="PENDENTE", timestamp=timestamp)],
            origem="manual",
        )

        self._repository.save(captura)

        return CapturaOutputDTO(
            sucesso=True,
            captura_id=captura_id,
            s3_key=s3_key,
            timestamp=timestamp,
            plantacao_id=MOCK_PLANTACAO_ID,
        )


class ClassifyCapturaUseCase:
    """
    Baixa a imagem da captura, manda pro modelo de nuvem (via
    IClassificationService) e atualiza a captura com o resultado.
    """

    def __init__(
        self,
        repository: ICapturaRepository,
        storage: IStorageService,
        classifier: IClassificationService,
        email_service: IEmailService | None = None,
        user_lookup: IUserLookupService | None = None,
    ):
        self._repository = repository
        self._storage = storage
        self._classifier = classifier
        self._email_service = email_service
        self._user_lookup = user_lookup

    def execute(self, plantacao_id: str, timestamp: str, captura_id: str) -> Captura:
        captura = self._repository.get(plantacao_id, timestamp, captura_id)
        if captura is None:
            raise ValueError(f"Captura não encontrada: {captura_id}")

        agora = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            image_bytes = self._storage.download_image(captura.s3_key)
            image_bytes = redimensionar_para_classificacao(image_bytes)
            resultado = self._classifier.classify(image_bytes)
        except Exception as e:
            captura.status = "ERRO"
            captura.erro_detalhes = str(e)
            captura.status_history.append(StatusEntry(status="ERRO", timestamp=agora))
            self._repository.update(captura)
            raise

        captura.ia_nuvem = resultado.to_dict()
        captura.status = "CLASSIFICADO"
        captura.erro_detalhes = None
        captura.status_history.append(StatusEntry(status="CLASSIFICADO", timestamp=agora))

        if resultado.status_geral == "nao_saudavel":
            captura.alerta_emitido = True
            captura.alerta_emitido_em = agora

        self._repository.update(captura)

        if (
            resultado.status_geral == "nao_milho"
            and captura.origem == "manual"
            and self._email_service is not None
            and self._user_lookup is not None
        ):
            self._avisar_captura_invalida(captura)

        return captura

    def _avisar_captura_invalida(self, captura: Captura) -> None:
        try:
            email = self._user_lookup.obter_email(captura.cliente_id)
            if not email:
                return

            assunto = "Blick — sua captura não foi reconhecida como milho"
            corpo = (
                f"Olá,\n\n"
                f"A foto que você enviou (captura {captura.captura_id}) não foi "
                f"reconhecida como uma planta de milho válida pelo nosso modelo "
                f"de classificação.\n\n"
                f"Isso costuma acontecer quando a foto está com pouca luz, "
                f"desfocada, tirada de um ângulo muito distante, ou não mostra "
                f"a planta claramente.\n\n"
                f"Recomendamos enviar uma nova captura, com boa iluminação e "
                f"enquadrando bem a folha ou a planta de milho.\n\n"
                f"— Equipe Blick"
            )
            self._email_service.enviar_email(email, assunto, corpo)
        except Exception:
            pass


class ClassifyPendentesUseCase:
    """Processa em lote as capturas que ainda estao com status PENDENTE."""

    def __init__(self, repository: ICapturaRepository, classify_use_case: ClassifyCapturaUseCase):
        self._repository = repository
        self._classify_use_case = classify_use_case

    def execute(self, limite: int = 50) -> dict:
        pendentes = self._repository.list_by_status(
            "PENDENTE", plantacao_id=MOCK_PLANTACAO_ID, limit=limite
        )
        processadas, erros = 0, 0

        for captura in pendentes:
            try:
                self._classify_use_case.execute(
                    captura.plantacao_id, captura.timestamp, captura.captura_id
                )
                processadas += 1
            except Exception:
                erros += 1

        return {"total": len(pendentes), "processadas": processadas, "erros": erros}


class ReclassificarTodasUseCase:
    """
    Reclassifica capturas em lote, INDEPENDENTE do status atual.
    """

    def __init__(self, repository: ICapturaRepository, classify_use_case: ClassifyCapturaUseCase):
        self._repository = repository
        self._classify_use_case = classify_use_case

    def execute(
        self,
        plantacao_id: str = MOCK_PLANTACAO_ID,
        pagina: int = 1,
        tamanho_pagina: int = 20,
    ) -> dict:
        capturas, total = self._repository.list_by_plantacao(
            plantacao_id=plantacao_id, pagina=pagina, tamanho_pagina=tamanho_pagina
        )
        processadas, erros = 0, 0

        for captura in capturas:
            try:
                self._classify_use_case.execute(
                    captura.plantacao_id, captura.timestamp, captura.captura_id
                )
                processadas += 1
            except Exception:
                erros += 1

        total_paginas = (total + tamanho_pagina - 1) // tamanho_pagina if total > 0 else 0
        return {
            "pagina": pagina,
            "tamanho_pagina": tamanho_pagina,
            "total": total,
            "total_paginas": total_paginas,
            "processadas": processadas,
            "erros": erros,
        }


class ListClientesUseCase:
    def __init__(self, repository: ICapturaRepository):
        self._repository = repository

    def execute(self) -> ListClientesOutputDTO:
        cliente_ids = self._repository.list_cliente_ids()
        return ListClientesOutputDTO(cliente_ids=cliente_ids)


def _resumo_de(captura: Captura, storage: IStorageService) -> CapturaResumoDTO:
    ia = captura.ia_nuvem or {}
    confianca_str = ia.get("confianca_status_geral")

    try:
        chave_imagem = captura.thumbnail_key or captura.s3_key
        imagem_url = storage.generate_presigned_url(chave_imagem)
    except Exception:
        imagem_url = None

    return CapturaResumoDTO(
        captura_id=captura.captura_id,
        timestamp=captura.timestamp,
        status=captura.status,
        status_geral=ia.get("status_geral"),
        confianca_status_geral=float(confianca_str) if confianca_str is not None else None,
        latitude=captura.coordenadas.latitude,
        longitude=captura.coordenadas.longitude,
        alerta_emitido=captura.alerta_emitido,
        imagem_url=imagem_url,
        origem=captura.origem,
    )


class ListCapturasUseCase:
    """
    GET geral — lista enxuta de capturas de uma plantacao, mais recentes
    primeiro por padrao. Suporta filtro por status_geral (saudavel/
    nao_saudavel/nao_milho), por periodo de data, e paginacao numerada
    (8 por pagina por padrao).
    """

    def __init__(self, repository: ICapturaRepository, storage: IStorageService):
        self._repository = repository
        self._storage = storage

    def execute(
        self,
        plantacao_id: str = MOCK_PLANTACAO_ID,
        status: str | None = None,
        status_geral: str | None = None,
        origem: str | None = None,
        data_inicio: str | None = None,
        data_fim: str | None = None,
        pagina: int = 1,
        tamanho_pagina: int = 8,
    ) -> ListCapturasOutputDTO:
        capturas, total = self._repository.list_by_plantacao(
            plantacao_id=plantacao_id,
            status=status,
            status_geral=status_geral,
            origem=origem,
            data_inicio=data_inicio,
            data_fim=data_fim,
            pagina=pagina,
            tamanho_pagina=tamanho_pagina,
            fl_treino=0,  # listagem esconde o que foi usado em treino/validacao
        )
        total_paginas = (total + tamanho_pagina - 1) // tamanho_pagina if total > 0 else 0
        return ListCapturasOutputDTO(
            capturas=[_resumo_de(c, self._storage) for c in capturas],
            pagina=pagina,
            tamanho_pagina=tamanho_pagina,
            total=total,
            total_paginas=total_paginas,
        )


LIMITE_PONTOS_MAPA = 20_000


class MapaLimiteExcedidoError(Exception):
    """Mais pontos do que a resposta do mapa suporta com seguranca."""


class ListMapaCapturasUseCase:
    """
    GET /capturas/mapa — pontos enxutos pra desenhar o mapa. Entrega so o
    que o mapa precisa: captura_id, timestamp (o detalhe exige ele na URL),
    latitude, longitude e status_geral. Nao gera imagem_url (cada URL
    assinada pesa ~2 KB e ninguem abre 3 mil fotos) — a foto vem do
    endpoint de detalhe quando o usuario seleciona o pin.
    """

    def __init__(self, repository: ICapturaRepository):
        self._repository = repository

    def execute(
        self,
        plantacao_id: str = MOCK_PLANTACAO_ID,
        status_geral: str | None = None,
        limite: int = LIMITE_PONTOS_MAPA,
    ) -> dict:
        itens = self._repository.list_pontos_mapa(
            plantacao_id=plantacao_id,
            status_geral=status_geral,
            fl_treino=0,  # mapa acompanha a listagem: sem fotos de treino/validacao
        )

        if len(itens) > limite:
            raise MapaLimiteExcedidoError(
                f"Mais de {limite} pontos para o mapa. Use o filtro statusGeral."
            )

        pontos = []
        for it in itens:
            lat, lon = it["latitude"], it["longitude"]
            if lat is None or lon is None:
                continue

            pontos.append(
                {
                    "captura_id": it["captura_id"],
                    "timestamp": it["timestamp"],
                    "latitude": round(float(lat), 6),
                    "longitude": round(float(lon), 6),
                    "status_geral": it["status_geral"],
                }
            )

        return {"total": len(pontos), "pontos": pontos}


class GetCapturaUseCase:
    """GET especifico — detalhe completo de uma captura."""

    def __init__(self, repository: ICapturaRepository, storage: IStorageService):
        self._repository = repository
        self._storage = storage

    def execute(
        self, plantacao_id: str, timestamp: str, captura_id: str
    ) -> CapturaDetalheDTO | None:
        captura = self._repository.get(plantacao_id, timestamp, captura_id)
        if captura is None:
            return None

        ia = captura.ia_nuvem or {}
        probabilidades = ia.get("probabilidades")
        if probabilidades is not None:
            probabilidades = {k: float(v) for k, v in probabilidades.items()}

        confianca_status_str = ia.get("confianca_status_geral")
        confianca_subtipo_str = ia.get("confianca_subtipo")

        try:
            imagem_url = self._storage.generate_presigned_url(captura.s3_key)
        except Exception:
            imagem_url = None

        return CapturaDetalheDTO(
            captura_id=captura.captura_id,
            plantacao_id=captura.plantacao_id,
            carrinho_id=captura.carrinho_id,
            cliente_id=captura.cliente_id,
            timestamp=captura.timestamp,
            status=captura.status,
            latitude=captura.coordenadas.latitude,
            longitude=captura.coordenadas.longitude,
            status_geral=ia.get("status_geral"),
            confianca_status_geral=(
                float(confianca_status_str) if confianca_status_str is not None else None
            ),
            subtipo=ia.get("subtipo"),
            confianca_subtipo=(
                float(confianca_subtipo_str) if confianca_subtipo_str is not None else None
            ),
            probabilidades=probabilidades,
            modelo_versao_borda=captura.jetson_nano.modelo_versao,
            confianca_borda=captura.jetson_nano.confianca,
            imagem_url=imagem_url,
            status_history=[
                {"status": e.status, "timestamp": e.timestamp} for e in captura.status_history
            ],
            erro_detalhes=captura.erro_detalhes,
            alerta_emitido=captura.alerta_emitido,
            origem=captura.origem,
            alerta_emitido_em=captura.alerta_emitido_em,
            analise_por_planta=ia.get("analise_por_planta"),
        )


class DeletarCapturaUseCase:
    """Exclusao definitiva de uma captura — apaga DynamoDB E S3."""

    def __init__(self, repository: ICapturaRepository, storage: IStorageService):
        self._repository = repository
        self._storage = storage

    def execute(self, plantacao_id: str, timestamp: str, captura_id: str) -> bool:
        captura = self._repository.get(plantacao_id, timestamp, captura_id)
        if captura is None:
            return False

        self._repository.delete(plantacao_id, timestamp, captura_id)

        try:
            self._storage.delete_image(captura.s3_key)
        except Exception:
            pass

        return True


class GerarThumbnailsUseCase:
    """Backfill: gera miniatura pra capturas ANTIGAS que nao tem thumbnail_key."""

    def __init__(self, repository: ICapturaRepository, storage: IStorageService):
        self._repository = repository
        self._storage = storage

    def execute(
        self,
        plantacao_id: str = MOCK_PLANTACAO_ID,
        pagina: int = 1,
        tamanho_pagina: int = 20,
    ) -> dict:
        capturas, total = self._repository.list_by_plantacao(
            plantacao_id=plantacao_id, pagina=pagina, tamanho_pagina=tamanho_pagina
        )
        processadas, ja_tinham, erros = 0, 0, 0

        for captura in capturas:
            if captura.thumbnail_key:
                ja_tinham += 1
                continue

            try:
                image_bytes = self._storage.download_image(captura.s3_key)
                thumbnail_bytes = gerar_thumbnail(image_bytes)
                if thumbnail_bytes is None:
                    erros += 1
                    continue

                thumbnail_key = captura.s3_key.rsplit(".", 1)[0] + "_thumb.jpg"
                self._storage.upload_image(thumbnail_key, thumbnail_bytes)

                captura.thumbnail_key = thumbnail_key
                self._repository.update(captura)
                processadas += 1
            except Exception:
                erros += 1

        total_paginas = (total + tamanho_pagina - 1) // tamanho_pagina if total > 0 else 0
        return {
            "pagina": pagina,
            "tamanho_pagina": tamanho_pagina,
            "total": total,
            "total_paginas": total_paginas,
            "processadas": processadas,
            "ja_tinham": ja_tinham,
            "erros": erros,
        }


class BackfillOrigemUseCase:
    """Backfill: escreve o campo "origem" explicitamente em capturas ANTIGAS."""

    def __init__(self, repository: ICapturaRepository):
        self._repository = repository

    def execute(
        self,
        plantacao_id: str = MOCK_PLANTACAO_ID,
        pagina: int = 1,
        tamanho_pagina: int = 50,
    ) -> dict:
        capturas, total = self._repository.list_by_plantacao(
            plantacao_id=plantacao_id, pagina=pagina, tamanho_pagina=tamanho_pagina
        )
        processadas, erros = 0, 0

        for captura in capturas:
            try:
                self._repository.update(captura)
                processadas += 1
            except Exception:
                erros += 1

        total_paginas = (total + tamanho_pagina - 1) // tamanho_pagina if total > 0 else 0
        return {
            "pagina": pagina,
            "tamanho_pagina": tamanho_pagina,
            "total": total,
            "total_paginas": total_paginas,
            "processadas": processadas,
            "erros": erros,
        }
