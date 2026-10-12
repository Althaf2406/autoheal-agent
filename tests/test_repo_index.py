import pytest

from backend.repo_index import RepoIndex


def test_parse_functions_and_classes():
    code = '''
"""Module docstring."""

class DataProcessor:
    """Class docstring."""
    def __init__(self, batch_size: int = 100):
        self.batch_size = batch_size

    def process(self, data: list):
        """Process method."""
        return len(data)

def calculate_revenue(price: float, quantity: int = 1) -> float:
    """Calculates total revenue."""
    return price * quantity
'''
    index = RepoIndex()
    count = index.index_files({"etl/processor.py": code})

    assert count == 1
    func = index.get_function("calculate_revenue")
    assert func is not None
    assert func.name == "calculate_revenue"
    assert func.file_path == "etl/processor.py"
    assert len(func.parameters) == 2
    assert func.parameters[0].name == "price"
    assert func.parameters[0].annotation == "float"
    assert func.parameters[1].name == "quantity"
    assert func.parameters[1].default == "1"
    assert "Calculates total revenue" in (func.docstring or "")

    cls = index.classes[0]
    assert cls.name == "DataProcessor"
    assert "process" in cls.methods


def test_caller_callee_relationship():
    code_a = '''
def helper_func(x):
    return x * 2

def compute_total(a, b):
    h = helper_func(a)
    return h + b
'''
    code_b = '''
from module_a import compute_total

def main_app():
    result = compute_total(10, 20)
    print(result)
'''
    index = RepoIndex()
    index.index_files({
        "pkg/module_a.py": code_a,
        "pkg/module_b.py": code_b
    })

    # Test callers of helper_func
    callers_helper = index.get_callers("helper_func")
    assert len(callers_helper) == 1
    assert callers_helper[0].caller_function == "compute_total"
    assert callers_helper[0].file_path == "pkg/module_a.py"

    # Test callers of compute_total (cross-file)
    callers_compute = index.get_callers("compute_total")
    assert len(callers_compute) == 1
    assert callers_compute[0].caller_function == "main_app"
    assert callers_compute[0].file_path == "pkg/module_b.py"

    # Test callees of compute_total
    callees = index.get_callees("compute_total")
    assert "helper_func" in callees


def test_incremental_indexing():
    files = {
        "a.py": "def foo(): return 1",
        "b.py": "def bar(): return 2",
    }
    index = RepoIndex()
    parsed_first = index.index_files(files, incremental=True)
    assert parsed_first == 2

    # Re-indexing unchanged files should parse 0 files
    parsed_second = index.index_files(files, incremental=True)
    assert parsed_second == 0

    # Modify only b.py
    files["b.py"] = "def bar(): return 42"
    parsed_third = index.index_files(files, incremental=True)
    assert parsed_third == 1
    assert index.get_function("bar").source_code == "def bar(): return 42"


def test_search_similar():
    files = {
        "finance.py": """
def calculate_tax(income: float, rate: float = 0.1) -> float:
    \"\"\"Calculates tax amount from income.\"\"\"
    return income * rate

def discount_price(price: float) -> float:
    return price * 0.9
"""
    }
    index = RepoIndex()
    index.index_files(files)

    results = index.search_similar("tax calculation income", top_k=1)
    assert len(results) == 1
    assert results[0].name == "calculate_tax"


def test_syntax_error_resilience():
    files = {
        "good.py": "def valid_function(): return True",
        "broken.py": "def broken_syntax( - invalid python! {",
    }
    index = RepoIndex()
    parsed = index.index_files(files)

    # Should successfully index good.py and not crash on broken.py
    assert parsed == 2
    assert index.get_function("valid_function") is not None
    assert index.get_function("broken_syntax") is None
