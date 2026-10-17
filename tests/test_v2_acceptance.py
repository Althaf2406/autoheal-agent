"""Plan V2 acceptance criteria that span both members' modules, run with the real Anggota A modules."""
import pytest

from backend import context_retriever, graph, guardrails, run_store
from backend.repo_index import RepoIndex
from conftest import ORIGINAL, PAYLOAD, engineer_result

# (log line, secret that must not survive) -- Sub-Task 9: 20+ toxic logs
TOXIC_LOGS = [
    ("botocore.exceptions.ClientError: key AKIAIOSFODNN7EXAMPLE denied", "AKIAIOSFODNN7EXAMPLE"),
    ("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    ("remote: invalid token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789", "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"),
    ("GH_TOKEN=gho_ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210", "gho_ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210"),
    ("token github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRS",
     "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRS"),
    ("auth failed for eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
     "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
    ("could not connect to postgresql://etl_user:Pg_s3cret!@db.prod.internal:5432/sales", "Pg_s3cret!"),
    ("OperationalError: mysql://root:hunter2@10.0.0.5/warehouse", "hunter2"),
    ("MongoServerError at mongodb+srv://svc:M0ng0Pass@cluster0.example.net/db", "M0ng0Pass"),
    ("Login failed: password=S3cr3tP@ssw0rd for user etl", "S3cr3tP@ssw0rd"),
    ('env: DB_PASSWORD="Pa55word!2024"', "Pa55word!2024"),
    ("requests.exceptions.HTTPError: 401 Authorization: Bearer 9f8e7d6c5b4a39281706f5e4d3c2b1a0",
     "9f8e7d6c5b4a39281706f5e4d3c2b1a0"),
    ("SLACK_BOT_TOKEN=xoxb-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx",
     "xoxb-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"),
    ("ValueError: bad row for customer budi.santoso@gmail.com", "budi.santoso@gmail.com"),
    ("row 17: email=siti.rahma@perusahaan.co.id,total=10", "siti.rahma@perusahaan.co.id"),
    ("ValueError: invalid phone +62 812-3456-7890 in row 3", "812-3456-7890"),
    ("ValueError: invalid phone 081234567890 in row 4", "081234567890"),
    ("KeyError: NIK 3201234567890001 not found", "3201234567890001"),
    ("card 4111 1111 1111 1111 declined", "4111 1111 1111 1111"),
    ("card 5500-0000-0000-0004 declined", "5500-0000-0000-0004"),
    ("api_key=sk-proj-Abc123Def456Ghi789Jkl012Mno345 rejected", "sk-proj-Abc123Def456Ghi789Jkl012Mno345"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
     "-----END RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"),
]


def run(payload):
    run_id = run_store.create_run({k: v for k, v in payload.items() if k != "error_log"})["id"]
    return graph.run_pipeline(run_id, payload), run_store.get_run(run_id)


def test_graph_uses_the_real_anggota_a_modules():
    assert graph.redact.__module__ == "backend.redactor"
    assert graph.acquire.__module__ == graph.fingerprint.__module__ == "backend.dedup"
    assert graph.retrieve_context.__module__ == graph.check_impact.__module__ == "backend.context_retriever"


@pytest.mark.parametrize("log, secret", TOXIC_LOGS, ids=[f"log{i}" for i in range(len(TOXIC_LOGS))])
def test_toxic_log_is_redacted(log, secret):
    result = graph.redact(log)
    assert secret not in result.text
    assert result.entity_count >= 1


def test_no_secret_reaches_the_llm_or_storage(fakes):
    """Sub-Task 9 + demo scenario 1: every toxic log in one failure report."""
    log = "\n".join(line for line, _ in TOXIC_LOGS) + "\nKeyError: 'total_price'"
    _, stored = run({**PAYLOAD, "error_log": log})

    sent = fakes.researcher_inputs + [call["error"] for call in fakes.engineer_calls]
    leaked = [secret for _, secret in TOXIC_LOGS if any(secret in text for text in sent) or secret in str(stored)]
    assert leaked == []


def test_demo_aws_key_and_customer_email_are_redacted(fakes):
    """Demo scenario 1 with the real redactor."""
    log = ("botocore error key=AKIAIOSFODNN7EXAMPLE while loading orders of budi.santoso@gmail.com\n"
           "KeyError: 'total_price'")
    final, stored = run({**PAYLOAD, "error_log": log})

    for text in (fakes.researcher_inputs[0], fakes.engineer_calls[0]["error"], str(stored)):
        assert "AKIAIOSFODNN7EXAMPLE" not in text and "budi.santoso@gmail.com" not in text
    assert stored["redaction_count"] == 2 and final["outcome"] == "pr"


# --- Sub-Task 18: impact analysis inside the pipeline ---

CALLER = "from etl.pipeline import transform\n\n\ndef main(df):\n    return transform(df)\n"
NEW_PARAM = ORIGINAL.replace("def transform(df):", "def transform(df, price_col):").replace(
    'df["total_price"]', "df[price_col]")
NEW_PARAM_WITH_DEFAULT = NEW_PARAM.replace("price_col)", 'price_col="price")')


@pytest.fixture
def indexed_repo():
    index = RepoIndex()
    index.index_files({"etl/pipeline.py": ORIGINAL, "main.py": CALLER})
    context_retriever.set_active_index(index)
    yield index
    context_retriever.set_active_index(None)


def test_signature_change_without_callers_is_rejected_then_fixed(fakes, indexed_repo):
    fakes.engineer_replies = [engineer_result(code=NEW_PARAM), engineer_result(code=NEW_PARAM_WITH_DEFAULT)]
    final, stored = run(PAYLOAD)

    assert stored["attempts"][0]["stage"] == "sast"
    assert "main.py" in stored["attempts"][0]["feedback"]
    assert "main.py" in fakes.engineer_calls[1]["feedback"]
    assert final["outcome"] == "pr" and final["iteration"] == 2


def test_engineer_gets_the_caller_as_context(fakes, indexed_repo):
    run(PAYLOAD)
    assert fakes.engineer_calls[0]["context_files"] == {"main.py": CALLER}


@pytest.mark.xfail(strict=True, reason="Anggota A: check_impact compares against the pre-patch index, "
                                       "so callers updated in the same patch still count as broken")
def test_signature_change_with_callers_updated_passes(indexed_repo):
    """Plan: a patch is allowed when every caller is updated in the same PR."""
    new_caller = CALLER.replace("transform(df)", 'transform(df, "price")')
    old = {"etl/pipeline.py": ORIGINAL, "main.py": CALLER}
    new = {"etl/pipeline.py": NEW_PARAM, "main.py": new_caller}
    result = guardrails.validate_patch(guardrails.make_diff(old, new), new, old,
                                       check_impact=context_retriever.check_impact)
    assert result.layers["impact"] == "passed", result.reasons
