import unittest

from local_selector import is_excluded_name, is_limit_close, is_mainboard, limit_price


class SelectorUtilityTests(unittest.TestCase):
    def test_mainboard_codes(self):
        self.assertTrue(is_mainboard("000001"))
        self.assertTrue(is_mainboard("600000"))
        self.assertFalse(is_mainboard("300001"))
        self.assertFalse(is_mainboard("688001"))

    def test_limit_price(self):
        self.assertEqual(limit_price(10.00), 11.00)
        self.assertEqual(limit_price(7.63), 8.39)

    def test_limit_close(self):
        self.assertTrue(is_limit_close(11.00, 10.00))
        self.assertTrue(is_limit_close(10.99, 10.00))
        self.assertFalse(is_limit_close(10.80, 10.00))

    def test_excluded_names(self):
        self.assertTrue(is_excluded_name("ST测试"))
        self.assertTrue(is_excluded_name("*ST测试"))
        self.assertTrue(is_excluded_name("退市测试"))
        self.assertFalse(is_excluded_name("平安银行"))


if __name__ == "__main__":
    unittest.main()
