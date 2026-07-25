from pathlib import Path


def test_generated_core_client_artifact_is_present():
    root = Path(__file__).resolve().parents[1]
    package = root / "generated" / "kogwistar_api" / "knowledge_engine_mcp_admin_client"
    assert (package / "client.py").is_file()
    assert (package / "api" / "chat" / "get_run_evidence_api_runs_run_id_evidence_get.py").is_file()


def test_graph_api_exposes_versioned_evidence_and_lifecycle_facade():
    from graph_api import GraphAPI

    names = set(dir(GraphAPI))
    assert {"get_run_steps", "get_run_checkpoints", "get_run_evidence", "get_resume_contract"} <= names
