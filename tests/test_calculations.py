import unittest
import sys
import os

# Add parent directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from exactmultifundchannel import (
    bitcoin_tx_core_weight,
    get_utxo_spend_weight,
    calculate_exact_allocation,
    parse_destinations,
    parse_sat_amount,
    normalize_params,
    DUST_LIMIT_SAT
)


class TestExactChannelFunding(unittest.TestCase):

    def test_core_weight(self):
        self.assertEqual(bitcoin_tx_core_weight(1, 1), 42)
        self.assertEqual(bitcoin_tx_core_weight(2, 3), 42)
        self.assertEqual(bitcoin_tx_core_weight(253, 1), (4 + 3 + 1 + 4) * 4 + 2)

    def test_utxo_spend_weight(self):
        # Native SegWit (P2WPKH) -> 271 WU
        self.assertEqual(get_utxo_spend_weight("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"), 271)
        # Taproot (P2TR) -> 230 WU
        self.assertEqual(get_utxo_spend_weight("bc1p5d7rx65gnslx2e4vs3pwc2hnwn9wpmratnyw2w9k9d9529sq4faqbh79ed"), 230)
        # Nested SegWit (P2SH) -> 363 WU
        self.assertEqual(get_utxo_spend_weight("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"), 363)

    def test_parse_sat_amount(self):
        self.assertEqual(parse_sat_amount(1000000), 1000000)
        self.assertEqual(parse_sat_amount("500000sat"), 500000)
        self.assertEqual(parse_sat_amount("1000000000msat"), 1000000)
        self.assertEqual(parse_sat_amount("0.02btc"), 2000000)

    def test_parse_destinations_json_strings_and_dicts(self):
        # List of JSON strings as in CLI invocation
        raw = ['{"id":"aaaaa", "amount":1000000}', '{"id":"bbbb", "amount":2000000}']
        parsed = parse_destinations(raw)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["id"], "aaaaa")
        self.assertEqual(parsed[0]["amount"], 1000000)
        self.assertEqual(parsed[1]["id"], "bbbb")
        self.assertEqual(parsed[1]["amount"], 2000000)

        # JSON encoded string of array
        raw_str = '[{"id": "c", "amount": 300}]'
        parsed_str = parse_destinations(raw_str)
        self.assertEqual(len(parsed_str), 1)
        self.assertEqual(parsed_str[0]["id"], "c")

    def test_change_go_to_index_1(self):
        """User's exact example: change_go_to=1 (node 'bbbb')"""
        # 2 UTXOs of 2,000,000 each = 4,000,000 sats (P2WPKH: 271 WU each)
        utxos = [
            {"outpoint": "xxxx:0", "amount_sat": 2000000, "address": "bc1qtest1"},
            {"outpoint": "yyyy:1", "amount_sat": 2000000, "address": "bc1qtest2"}
        ]
        destinations = [
            {"id": "aaaaa", "amount": 1000000},
            {"id": "bbbb", "amount": 2000000}
        ]
        feerate_per_kw = 700  # from user example

        # Total weight:
        # Core: 42
        # Inputs: 2 * 271 = 542
        # Outputs: 2 * 172 = 344
        # Total = 42 + 542 + 344 = 928 WU
        expected_weight = 42 + 542 + 344
        self.assertEqual(expected_weight, 928)

        # Fee: (700 * 928) // 1000 = 649 sats
        expected_fee = (700 * 928) // 1000
        self.assertEqual(expected_fee, 649)

        # Node 'bbbb' (index 1) gets:
        # Total input (4,000,000) - aaaaa (1,000,000) - fee (649) = 2,999,351 sats
        # Base was 2,000,000, so change absorbed is 999,351 sats!
        expected_bbbb_amount = 4000000 - 1000000 - 649
        self.assertEqual(expected_bbbb_amount, 2999351)

        res = calculate_exact_allocation(destinations, feerate_per_kw, utxos, change_go_to_idx=1)
        self.assertEqual(res["exact_target_amount"], expected_bbbb_amount)
        self.assertEqual(res["exact_fee"], expected_fee)
        self.assertEqual(res["change_absorbed"], 999351)
        self.assertEqual(res["change_go_to_id"], "bbbb")

        # Zero-change invariant: Inputs - Outputs == Fee
        total_outs = 1000000 + res["exact_target_amount"]
        self.assertEqual(4000000 - total_outs, res["exact_fee"])

    def test_change_go_to_index_0(self):
        """Default change_go_to=0 (node 'aaaaa')"""
        utxos = [{"outpoint": "tx1:0", "amount_sat": 1500000, "address": "bc1qtest1"}]
        destinations = [
            {"id": "aaaaa", "amount": 500000},
            {"id": "bbbb", "amount": 500000}
        ]
        feerate_per_kw = 2500

        res = calculate_exact_allocation(destinations, feerate_per_kw, utxos, change_go_to_idx=0)
        self.assertEqual(res["change_go_to_id"], "aaaaa")
        # aaaaa amount: 1500000 - 500000 - fee
        expected_fee = res["exact_fee"]
        self.assertEqual(res["exact_target_amount"], 1000000 - expected_fee)
        self.assertEqual(res["total_inputs"] - (res["exact_target_amount"] + 500000), expected_fee)

    def test_out_of_bounds_change_go_to(self):
        utxos = [{"outpoint": "tx1:0", "amount_sat": 1000000, "address": "bc1qtest1"}]
        destinations = [{"id": "node1", "amount": 100000}]
        with self.assertRaises(ValueError) as ctx:
            calculate_exact_allocation(destinations, 2500, utxos, change_go_to_idx=2)
        self.assertIn("out of bounds", str(ctx.exception).lower())

    def test_normalize_params_preserves_extra_arguments(self):
        # Keyword arguments including unexpected future parameters
        kw = {
            "destinations": [{"id": "a", "amount": 100}],
            "feerate": 700,
            "utxos": ["x:0"],
            "change_go_to": 0,
            "future_cln_param_xyz": "future_value",
            "another_param": 12345
        }
        normalized = normalize_params(kw)
        self.assertIn("future_cln_param_xyz", normalized)
        self.assertEqual(normalized["future_cln_param_xyz"], "future_value")
        self.assertEqual(normalized["another_param"], 12345)


if __name__ == "__main__":
    unittest.main()
