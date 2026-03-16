from coordinator.sanitizer import detect_repetition_issues, sanitize_generated_code


def test_removes_repeated_python_main_blocks():
    source = """
class LibraryManagementSystem:
    def run(self):
        print("ok")

if __name__ == "__main__":
    system = LibraryManagementSystem()
    system.run()

if __name__ == "__main__":
    system = LibraryManagementSystem()
    system.run()

if __name__ == "__main__":
    system = LibraryManagementSystem()
    system.run()
""".strip()

    cleaned = sanitize_generated_code(source, language="python")

    assert cleaned is not None
    assert cleaned.count('if __name__ == "__main__":') == 1
    assert cleaned.count("system = LibraryManagementSystem()") == 1
    assert cleaned.count("system.run()") == 1


def test_removes_repeated_tail_block():
    repeated = "\n".join(
        [
            "def add(a, b):",
            "    return a + b",
            "",
            "def subtract(a, b):",
            "    return a - b",
            "",
        ]
    )
    source = f"{repeated}\n{repeated}\n{repeated}"

    cleaned = sanitize_generated_code(source, language="python")

    assert cleaned is not None
    assert cleaned.count("def add(a, b):") == 1
    assert cleaned.count("def subtract(a, b):") == 1


def test_detects_repetition_issues():
    source = """
if __name__ == "__main__":
    system = LibraryManagementSystem()
    system.run()
if __name__ == "__main__":
    system = LibraryManagementSystem()
    system.run()
""".strip()

    issues = detect_repetition_issues(source, language="python")
    assert issues


def test_trims_mutated_fibonacci_repetition_tail():
    source = """
def fibonacci(n: int) -> int:
    if n < 2:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b

def main() -> None:
    print(fibonacci(10))

if __name__ == "__main__":
    main()

def fibonacci(n: int) -> int:
    if n < 2:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b

def main() -> None:
    return b

def main() -> None:
    print(fibonacci(10))
""".strip()

    cleaned = sanitize_generated_code(source, language="python")

    assert cleaned is not None
    assert cleaned.count("def fibonacci(") == 1
    assert cleaned.count("def main(") == 1
    assert cleaned.count('if __name__ == "__main__":') == 1


def test_detects_and_removes_duplicate_methods_inside_same_class():
    source = """
class Inventory:
    def __init__(self):
        self.items = {}

    def add_item(self, name, quantity):
        if quantity < 0:
            raise ValueError("Quantity cannot be less than 0")
        self.items[name] = quantity

    def drop_item(self, name, quantity):
        if quantity < 0:
            raise ValueError("Quantity cannot be less than 0")
        del self.items[name]

    def add_item(self, name, quantity):
        if quantity < 0:
            raise ValueError("Quantity cannot be less than 0")
        self.items[name] = quantity

    def drop_item(self, name, quantity):
        if quantity <= 0:
            raise ValueError("Quantity cannot be less than 0")
        del self.items[name]
""".strip()

    issues = detect_repetition_issues(source, language="python")
    cleaned = sanitize_generated_code(source, language="python")

    assert any("duplicate Python methods" in issue for issue in issues)
    assert cleaned is not None
    assert cleaned.count("def add_item(") == 1
    assert cleaned.count("def drop_item(") == 1

def test_trims_repeated_top_level_structure_even_when_names_change():
    source = """
class Inventory:
    def __init__(self):
        self.items = {}

    def add_item(self, name, quantity):
        if quantity < 0:
            raise ValueError("Quantity cannot be less than 0")
        self.items[name] = quantity


def run_cli():
    inv = Inventory()
    inv.add_item("apple", 2)


if __name__ == "__main__":
    run_cli()


class StockManager:
    def __init__(self):
        self.items = {}

    def add_item(self, product_name, amount):
        if amount < 0:
            raise ValueError("Quantity cannot be less than 0")
        self.items[product_name] = amount


def execute_cli():
    manager = StockManager()
    manager.add_item("apple", 2)


if __name__ == "__main__":
    execute_cli()
""".strip()

    issues = detect_repetition_issues(source, language="python")
    cleaned = sanitize_generated_code(source, language="python")

    assert any("repeated top-level code structure" in issue.lower() for issue in issues)
    assert cleaned is not None
    assert cleaned.count('if __name__ == "__main__":') == 1
    assert "class StockManager:" not in cleaned
    assert "def execute_cli():" not in cleaned
