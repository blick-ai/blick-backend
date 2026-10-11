import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _TabelaFalsa:
    """Registra os parametros de cada consulta, sem tocar na AWS."""

    def __init__(self, itens_scan=None):
        self.queries = []
        self.scans = []
        self._itens_scan = itens_scan or []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return {"Items": [], "Count": 0}

    def scan(self, **kwargs):
        self.scans.append(kwargs)
        # so o segmento 0 devolve os itens, pra nao duplicar entre segmentos
        itens = self._itens_scan if kwargs.get("Segment") == 0 else []
        return {"Items": itens, "Count": len(itens)}


def _repo(itens_scan=None):
    pytest.importorskip("boto3")
    from infrastructure.dynamo_repository import DynamoCapturaRepository

    repo = object.__new__(DynamoCapturaRepository)
    repo._table = _TabelaFalsa(itens_scan)
    return repo


def test_fl_treino_zero_inclui_itens_sem_a_coluna():
    repo = _repo()
    repo.list_by_plantacao(plantacao_id="p1", fl_treino=0)
    for kw in repo._table.queries + repo._table.scans:
        assert "attribute_not_exists(fl_treino)" in kw["FilterExpression"]
        assert kw["ExpressionAttributeValues"][":flt"] == 0


def test_fl_treino_um_filtra_so_as_de_treino():
    repo = _repo()
    repo.list_by_plantacao(plantacao_id="p1", fl_treino=1)
    for kw in repo._table.queries + repo._table.scans:
        assert "fl_treino = :flt" in kw["FilterExpression"]
        assert kw["ExpressionAttributeValues"][":flt"] == 1


def test_sem_fl_treino_nao_filtra_nada():
    repo = _repo()
    repo.list_by_plantacao(plantacao_id="p1")
    for kw in repo._table.queries + repo._table.scans:
        assert "fl_treino" not in kw.get("FilterExpression", "")
        assert ":flt" not in kw["ExpressionAttributeValues"]


def test_contagem_roda_em_varredura_paralela_e_vai_pro_cache():
    from infrastructure.dynamo_repository import SEGMENTOS_VARREDURA

    repo = _repo(itens_scan=[{"x": 1}, {"x": 2}])
    _, total = repo.list_by_plantacao(plantacao_id="p1", fl_treino=0)
    assert total == 2
    assert len(repo._table.scans) == SEGMENTOS_VARREDURA
    assert all(kw["Select"] == "COUNT" for kw in repo._table.scans)
    assert all("PK = :pk" in kw["FilterExpression"] for kw in repo._table.scans)

    repo.list_by_plantacao(plantacao_id="p1", fl_treino=0)
    assert len(repo._table.scans) == SEGMENTOS_VARREDURA  # 2a chamada veio do cache


def test_escrita_limpa_o_cache_da_contagem():
    from infrastructure.dynamo_repository import SEGMENTOS_VARREDURA

    repo = _repo(itens_scan=[{"x": 1}])
    repo.list_by_plantacao(plantacao_id="p1")
    repo._limpar_cache()
    repo.list_by_plantacao(plantacao_id="p1")
    assert len(repo._table.scans) == 2 * SEGMENTOS_VARREDURA


def test_pontos_do_mapa_vem_enxutos_filtrados_e_ordenados():
    itens = [
        {"captura_id": "a", "timestamp": "2026-09-29T10:00:00",
         "coordenadas": {"latitude": 1, "longitude": 2},
         "ia_nuvem": {"status_geral": "saudavel"}},
        {"captura_id": "b", "timestamp": "2026-09-30T10:00:00",
         "coordenadas": {"latitude": 3, "longitude": 4}},
    ]
    repo = _repo(itens_scan=itens)
    pontos = repo.list_pontos_mapa(plantacao_id="p1", fl_treino=0)

    assert [p["captura_id"] for p in pontos] == ["b", "a"]  # mais recente primeiro
    assert pontos[1]["status_geral"] == "saudavel"
    assert pontos[0]["status_geral"] is None
    kw = repo._table.scans[0]
    assert "contorno" not in kw["ProjectionExpression"]
    assert "analise_por_planta" not in kw["ProjectionExpression"]
    assert "attribute_not_exists(fl_treino)" in kw["FilterExpression"]
    assert kw["ExpressionAttributeValues"][":pk"] == "PLANT#p1"


class _RepoFalso:
    def __init__(self):
        self.chamadas = []

    def list_by_plantacao(self, **kwargs):
        self.chamadas.append(kwargs)
        return [], 0

    def list_pontos_mapa(self, **kwargs):
        self.chamadas.append(kwargs)
        return []


def test_listagem_e_mapa_escondem_treino_e_manutencao_nao():
    pytest.importorskip("PIL")
    from application.use_cases import (
        ListCapturasUseCase,
        ListMapaCapturasUseCase,
        ReclassificarTodasUseCase,
    )

    repo = _RepoFalso()
    ListCapturasUseCase(repo, storage=None).execute()
    ListMapaCapturasUseCase(repo).execute()
    ReclassificarTodasUseCase(repo, classify_use_case=None).execute()

    listagem, mapa, manutencao = repo.chamadas
    assert listagem["fl_treino"] == 0
    assert mapa["fl_treino"] == 0
    assert "fl_treino" not in manutencao  # a reclassificacao precisa ver tudo


def test_mapa_monta_pontos_e_pula_sem_coordenada():
    pytest.importorskip("PIL")
    from application.use_cases import ListMapaCapturasUseCase

    class Repo:
        def list_pontos_mapa(self, **kwargs):
            return [
                {"captura_id": "a", "timestamp": "t1", "latitude": 1.23456789,
                 "longitude": 2, "status_geral": "saudavel"},
                {"captura_id": "b", "timestamp": "t2", "latitude": None,
                 "longitude": None, "status_geral": None},
            ]

    r = ListMapaCapturasUseCase(Repo()).execute()
    assert r["total"] == 1
    assert r["pontos"][0]["latitude"] == 1.234568
