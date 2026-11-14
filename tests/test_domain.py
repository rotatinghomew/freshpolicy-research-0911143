import unittest
from freshpolicy.catalog import Catalog
from freshpolicy.domain import historical_service


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")

    def test_missing_requests_do_not_become_sales(self):
        totals = historical_service(self.catalog)
        self.assertEqual(sum(record["missing_requests"] for record in totals.values()), sum(record["requested_kg"] is None for record in self.catalog.rows("demand")))
        self.assertTrue(all(record["missing_requests"] > 0 for record in totals.values()))


    def test_zero_request_is_known(self):
        catalog = FakeCatalog({"demand": [{"sku": "鱼", "sold_kg": 0, "requested_kg": 0}]})
        self.assertEqual(historical_service(catalog)["鱼"]["missing_requests"], 0)

    def test_missing_request_is_not_imputed_from_sales(self):
        catalog = FakeCatalog({"demand": [{"sku": "鱼", "sold_kg": 5, "requested_kg": None}]})
        self.assertEqual(historical_service(catalog)["鱼"], {"sold_kg": 5, "known_requested_kg": 0, "missing_requests": 1})

    def test_empty_demand(self):
        self.assertEqual(historical_service(FakeCatalog({"demand": []})), {})

    def test_receipts_have_positive_remaining_life(self):
        self.assertTrue(all(record["expires_at"] > record["available_at"] for record in self.catalog.rows("receipts")))


class FakeCatalog:
    def __init__(self, tables):
        self.tables = tables

    def rows(self, name):
        return self.tables[name]
