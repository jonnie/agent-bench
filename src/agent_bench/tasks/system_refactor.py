"""Built-in system-refactor benchmark fixture. Candidate source is data."""

from .base import create_task

TASK = create_task(
    "system-refactor",
    "Centralize pricing across checkout and reporting",
    """
        Refactor the duplicated pricing policy in solution.checkout and
        solution.reports into solution.pricing.line_total(quantity, unit_price,
        discount_pct='0') -> Decimal. Keep checkout.order_total(lines) -> Decimal
        and reports.total_by_sku(lines) -> dict[str, Decimal]. Each line has sku,
        quantity, unit_price, and optional discount_pct. Valid quantity is a
        nonnegative int (bool is invalid). Price and discount are finite decimal
        strings, price >= 0 and discount in [0, 100]; reject any invalid argument
        with ValueError. Inputs have <= 12 significant digits and quantity <= 1000.
        line_total computes quantity * price * (1 - discount / 100), quantized
        to Decimal('0.01') with ROUND_HALF_EVEN PER LINE. Empty orders yield
        Decimal('0.00'); reports accumulate rounded lines by sku. Do not mutate.
        Both clients must import the pricing MODULE and call pricing.line_total
        dynamically for each line (including optional discounts), so policy
        changes and monkeypatches propagate. Monetary multiplication/division
        and Decimal quantization must exist only in pricing.py, not the clients.
        Do not leave unused duplicate policy code. Hidden tests inspect client
        ASTs and patch the central function as well as checking behavior.
        Run public tests with python -m unittest discover -s tests.
        """,
    {
        "pricing.py": """
                from decimal import Decimal

                CENT = Decimal('0.01')
            """,
        "checkout.py": """
                from decimal import Decimal, ROUND_HALF_EVEN

                def order_total(lines):
                    total = Decimal('0.00')
                    for line in lines:
                        amount = Decimal(line['quantity']) * Decimal(line['unit_price'])
                        amount *= 1 - Decimal(line.get('discount_pct', '0')) / 100
                        total += amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
                    return total
            """,
        "reports.py": """
                from decimal import Decimal, ROUND_HALF_EVEN

                def total_by_sku(lines):
                    totals = {}
                    for line in lines:
                        amount = Decimal(line['quantity']) * Decimal(line['unit_price'])
                        amount *= 1 - Decimal(line.get('discount_pct', '0')) / 100
                        rounded = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
                        sku = line['sku']
                        totals[sku] = totals.get(sku, Decimal('0.00')) + rounded
                    return totals
            """,
    },
    """
        import unittest
        from decimal import Decimal
        from solution.checkout import order_total
        from solution.reports import total_by_sku

        class PricingTests(unittest.TestCase):
            def test_existing_totals(self):
                lines = [{'sku': 'tea', 'quantity': 2, 'unit_price': '3.50'}]
                self.assertEqual(order_total(lines), Decimal('7.00'))
                self.assertEqual(total_by_sku(lines), {'tea': Decimal('7.00')})
        """,
    """
        import ast
        import copy
        import random
        import unittest
        from decimal import Decimal, ROUND_HALF_EVEN
        from pathlib import Path
        from unittest.mock import patch
        from solution import checkout, reports, pricing

        class PricingContract(unittest.TestCase):
            def test_canonical_policy(self):
                self.assertTrue(callable(getattr(pricing, 'line_total', None)), 'pricing.line_total is required')
                self.assertEqual(pricing.line_total(1, '1.005'), Decimal('1.00'))
                self.assertEqual(pricing.line_total(2, '10', '12.5'), Decimal('17.50'))
                self.assertIsInstance(pricing.line_total(0, '10'), Decimal)

            def test_validation_is_shared(self):
                bad = [(-1, '1', '0'), (True, '1', '0'), (1.5, '1', '0'),
                       (1, '-0.01', '0'), (1, 'NaN', '0'), (1, 'Infinity', '0'),
                       (1, 'nonsense', '0'), (1, '1', '-1'), (1, '1', '100.01'),
                       (1, '1', 'NaN'), (1, '1', 'Infinity'), (1, 1.0, '0'),
                       (1, '1', None)]
                for quantity, price, discount in bad:
                    line = {'sku': 'bad', 'quantity': quantity, 'unit_price': price,
                            'discount_pct': discount}
                    with self.subTest(line=line):
                        with self.assertRaises(ValueError):
                            pricing.line_total(quantity, price, discount)
                        with self.assertRaises(ValueError):
                            checkout.order_total([line])
                        with self.assertRaises(ValueError):
                            reports.total_by_sku([line])

            def test_existing_behavior_and_seeded_orders(self):
                self.assertEqual(checkout.order_total([]), Decimal('0.00'))
                self.assertEqual(reports.total_by_sku([]), {})
                rng = random.Random(1942)
                for _ in range(35):
                    lines = [{'sku': rng.choice(['tea', 'coffee', 'bread']),
                              'quantity': rng.randrange(20),
                              'unit_price': str(Decimal(rng.randrange(10000)) / 1000),
                              'discount_pct': str(rng.randrange(101))}
                             for _ in range(rng.randrange(1, 30))]
                    before = copy.deepcopy(lines)
                    totals = {}
                    for line in lines:
                        amount = (Decimal(line['quantity']) * Decimal(line['unit_price']) *
                                  (1 - Decimal(line['discount_pct']) / 100))
                        amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
                        totals[line['sku']] = totals.get(line['sku'], Decimal('0.00')) + amount
                    self.assertEqual(reports.total_by_sku(lines), totals)
                    self.assertEqual(checkout.order_total(lines), sum(totals.values(), Decimal('0.00')))
                    self.assertEqual(lines, before)

            def test_dynamic_delegation(self):
                lines = [{'sku': 'a', 'quantity': 2, 'unit_price': '10'},
                         {'sku': 'a', 'quantity': 1, 'unit_price': '5', 'discount_pct': '10'}]
                with patch.object(pricing, 'line_total', return_value=Decimal('7.00')) as policy:
                    self.assertEqual(checkout.order_total(lines), Decimal('14.00'))
                    self.assertEqual(policy.call_count, 2)
                    policy.reset_mock()
                    self.assertEqual(reports.total_by_sku(lines), {'a': Decimal('14.00')})
                    self.assertEqual(policy.call_count, 2)

            def test_architecture_not_just_behavior(self):
                for module in (checkout, reports):
                    tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
                    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
                    self.assertTrue(any(isinstance(call.func, ast.Attribute)
                                        and isinstance(call.func.value, ast.Name)
                                        and call.func.value.id == 'pricing'
                                        and call.func.attr == 'line_total' for call in calls),
                                    'clients must call pricing.line_total')
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.BinOp, ast.AugAssign)):
                            self.assertNotIsInstance(node.op, (ast.Mult, ast.Div, ast.FloorDiv, ast.Pow))
                        if isinstance(node, ast.Attribute):
                            self.assertNotEqual(node.attr, 'quantize', 'duplicate rounding policy')
                        if isinstance(node, ast.ImportFrom):
                            self.assertFalse(any(alias.name == 'line_total' for alias in node.names),
                                             'import the module, not a bound function')
        """,
)
