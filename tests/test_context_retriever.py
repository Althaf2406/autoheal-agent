import pytest

from backend import context_retriever, guardrails
from backend.repo_index import RepoIndex


@pytest.fixture(autouse=True)
def clean_active_index():
    context_retriever.set_active_index(None)
    yield
    context_retriever.set_active_index(None)


def test_retrieve_context_finds_callers_and_callees():
    pipeline_code = '''
def helper_clean(df):
    return df

def transform_data(df):
    cleaned = helper_clean(df)
    return cleaned
'''
    main_code = '''
from etl.pipeline import transform_data

def run_pipeline():
    data = [1, 2, 3]
    output = transform_data(data)
    return output
'''
    repo_files = {
        "etl/pipeline.py": pipeline_code,
        "main.py": main_code,
    }
    index = RepoIndex()
    index.index_files(repo_files)

    bundle = context_retriever.retrieve_context("etl/pipeline.py", repo_index=index)

    # Callers of transform_data should include run_pipeline in main.py
    assert any("main.py" in c and "run_pipeline" in c for c in bundle.callers)
    # Callees should include helper_clean
    assert "helper_clean" in bundle.callees
    # Related files should contain main.py
    assert "main.py" in bundle.files


def test_check_impact_detects_added_required_param():
    old_code = '''
def calculate_metrics(sales):
    return sum(sales)
'''
    new_code = '''
def calculate_metrics(sales, multiplier):
    return sum(sales) * multiplier
'''
    caller_code = '''
from metrics import calculate_metrics

def generate_report():
    return calculate_metrics([10, 20])
'''
    index = RepoIndex()
    index.index_files({
        "metrics.py": old_code,
        "report.py": caller_code,
    })

    broken = context_retriever.check_impact(old_code, new_code, repo_index=index)
    assert len(broken) == 1
    assert "report.py" in broken[0]
    assert "requires 2 arguments but call site provides 1" in broken[0]


def test_check_impact_allows_default_parameter():
    old_code = '''
def calculate_metrics(sales):
    return sum(sales)
'''
    new_code = '''
def calculate_metrics(sales, multiplier: float = 1.0):
    return sum(sales) * multiplier
'''
    caller_code = '''
def generate_report():
    return calculate_metrics([10, 20])
'''
    index = RepoIndex()
    index.index_files({
        "metrics.py": old_code,
        "report.py": caller_code,
    })

    broken = context_retriever.check_impact(old_code, new_code, repo_index=index)
    # Adding an optional parameter with default does not break existing callers
    assert broken == []


def test_check_impact_detects_removed_function():
    old_code = '''
def clean_records(records):
    return records
'''
    new_code = '''
def sanitize_records(records):
    return records
'''
    caller_code = '''
def process():
    return clean_records([])
'''
    index = RepoIndex()
    index.index_files({
        "cleaner.py": old_code,
        "worker.py": caller_code,
    })

    broken = context_retriever.check_impact(old_code, new_code, repo_index=index)
    assert len(broken) == 1
    assert "worker.py" in broken[0]
    assert "clean_records" in broken[0]


def test_guardrails_integration_with_impact():
    old_code = "def transform(df):\n    return df\n"
    new_broken_code = "def transform(df, config):\n    return df\n"
    caller_code = "def main():\n    return transform(data)\n"

    index = RepoIndex()
    index.index_files({
        "etl/pipeline.py": old_code,
        "main.py": caller_code,
    })
    context_retriever.set_active_index(index)

    diff = guardrails.make_diff({"etl/pipeline.py": old_code}, {"etl/pipeline.py": new_broken_code})
    result = guardrails.validate_patch(
        diff,
        new_files={"etl/pipeline.py": new_broken_code},
        old_files={"etl/pipeline.py": old_code},
        check_impact=context_retriever.check_impact,
    )

    assert result.passed is False
    assert result.layers["impact"] == "failed"
    assert any("main.py" in reason for reason in result.reasons)
