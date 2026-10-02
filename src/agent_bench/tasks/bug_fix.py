"""Built-in bug-fix benchmark fixture. Candidate source is data."""

from .base import create_task

TASK = create_task(
    "bug-fix",
    "Fix invoice reconciliation precision",
    """
        Fix solution.billing.invoice_total(items), used to reconcile invoice exports.
        Each item is a dict with quantity (a signed integer; negatives are refunds),
        unit_price (a nonnegative finite decimal string), and optional discount_pct
        (a finite decimal string in [0, 100], default '0'). Inputs are valid.
        Compute sum(quantity * unit_price * (1 - discount_pct / 100)) using decimal
        arithmetic, then round ONCE at the invoice boundary to cents using
        ROUND_HALF_EVEN. Return a fixed two-decimal string, normalizing rounded
        negative zero to '0.00'. Empty invoices return '0.00'. Do not mutate inputs.
        Monetary intermediates must not pass through float. Decimal inputs have
        at most 12 significant digits; invoices have at most 1,000 items with
        abs(quantity) <= 1,000, so the standard Decimal precision is sufficient.
        Keep the API; add regression coverage for fractional cents and refunds.
        Run public tests with python -m unittest discover -s tests.
        """,
    {
        "billing.py": """
            def invoice_total(items):
                total = 0.0
                for item in items:
                    amount = item['quantity'] * float(item['unit_price'])
                    amount *= 1 - float(item.get('discount_pct', '0')) / 100
                    total += round(amount, 2)
                return f'{total:.2f}'
        """
    },
    """
        import unittest
        from solution.billing import invoice_total

        class InvoiceTests(unittest.TestCase):
            def test_basic(self):
                self.assertEqual(invoice_total([{'quantity': 2, 'unit_price': '12.50'}]), '25.00')

            def test_round_at_invoice_boundary(self):
                rows = [{'quantity': 1, 'unit_price': '0.014'}] * 3
                self.assertEqual(invoice_total(rows), '0.04')
        """,
    """
        import copy
        import random
        import unittest
        from decimal import Decimal, ROUND_HALF_EVEN
        from solution.billing import invoice_total

        def expected(rows):
            total = sum((Decimal(r['quantity']) * Decimal(r['unit_price']) *
                         (Decimal(1) - Decimal(r.get('discount_pct', '0')) / 100)
                         for r in rows), Decimal(0))
            rounded = total.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
            return '0.00' if not rounded else format(rounded, '.2f')

        class InvoiceContract(unittest.TestCase):
            def test_existing_whole_cent_behavior(self):
                self.assertEqual(invoice_total([{'quantity': 4, 'unit_price': '12.50',
                                                'discount_pct': '20'}]), '40.00')
                self.assertEqual(invoice_total([]), '0.00')

            def test_fractional_cent_accumulation(self):
                self.assertEqual(invoice_total([{'quantity': 1, 'unit_price': '0.014'}] * 3), '0.04')

            def test_half_even_and_zero(self):
                for price, quantity, answer in [('1.005', 1, '1.00'), ('1.015', 1, '1.02'),
                                                ('0.004', -1, '0.00'), ('1.015', -1, '-1.02')]:
                    with self.subTest(price=price, quantity=quantity):
                        self.assertEqual(invoice_total([{'quantity': quantity, 'unit_price': price}]), answer)

            def test_refunds_discounts_and_large_values(self):
                rows = [{'quantity': 999, 'unit_price': '123456789.123', 'discount_pct': '12.345'},
                        {'quantity': -21, 'unit_price': '100.003', 'discount_pct': '99.999'},
                        {'quantity': 2, 'unit_price': '999.99', 'discount_pct': '100'}]
                self.assertEqual(invoice_total(rows), expected(rows))

            def test_seeded_invoices_and_no_mutation(self):
                rng = random.Random(81021)
                for _ in range(80):
                    rows = [{'quantity': rng.randint(-20, 20),
                             'unit_price': str(Decimal(rng.randrange(1000000)) / 1000),
                             'discount_pct': str(Decimal(rng.randrange(100001)) / 1000)}
                            for _ in range(rng.randrange(1, 30))]
                    before = copy.deepcopy(rows)
                    self.assertEqual(invoice_total(rows), expected(rows))
                    self.assertEqual(rows, before)
        """,
)
