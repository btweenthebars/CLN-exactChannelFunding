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
    process_exact_funding,
    check_wumbo_support,
    DUST_LIMIT_SAT,
    MAX_NON_WUMBO_SAT
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

    def test_parse_destinations(self):
        raw = ['{"id":"aaaaa", "amount":1000000}', '{"id":"bbbb", "amount":2000000}']
        parsed = parse_destinations(raw)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["id"], "aaaaa")
        self.assertEqual(parsed[0]["amount"], 1000000)
        self.assertEqual(parsed[1]["id"], "bbbb")
        self.assertEqual(parsed[1]["amount"], 2000000)

    def test_wumbo_bit_detection(self):
        # Bit 19 is 0x080000 in big-endian 3-byte hex
        features_bit19 = "080000"
        self.assertTrue(check_wumbo_support(features_bit19))

        # Bit 18 is 0x040000 in big-endian 3-byte hex
        features_bit18 = "040000"
        self.assertTrue(check_wumbo_support(features_bit18))

        # No wumbo bits (e.g. 0x020000 or 0x00)
        self.assertFalse(check_wumbo_support("020000"))
        self.assertFalse(check_wumbo_support("00"))
        self.assertFalse(check_wumbo_support(None))

    def test_change_to_index_1(self):
        """User's exact example: change_to=1 (node 'bbbb')"""
        utxos = [
            {"outpoint": "xxxx:0", "amount_sat": 2000000, "address": "bc1qtest1"},
            {"outpoint": "yyyy:1", "amount_sat": 2000000, "address": "bc1qtest2"}
        ]
        destinations = [
            {"id": "aaaaa", "amount": 1000000},
            {"id": "bbbb", "amount": 2000000}
        ]
        feerate_per_kw = 700

        expected_weight = 42 + (2 * 271) + (2 * 172)  # 928 WU
        expected_fee = (700 * 928) // 1000            # 649 sats
        expected_bbbb_amount = 4000000 - 1000000 - expected_fee

        res = calculate_exact_allocation(destinations, feerate_per_kw, utxos, change_to_idx=1)
        self.assertEqual(res["exact_target_amount"], expected_bbbb_amount)
        self.assertEqual(res["exact_fee"], expected_fee)
        self.assertEqual(res["change_absorbed"], 999351)
        self.assertEqual(res["change_to_id"], "bbbb")

        total_outs = 1000000 + res["exact_target_amount"]
        self.assertEqual(4000000 - total_outs, res["exact_fee"])

    def test_wumbo_limit_error_raised_when_non_wumbo(self):
        """Case 1: Channel > 16.7M sats without Wumbo support must raise an error."""
        # 1 UTXO of 20,000,000 sats (> 16,777,215)
        utxos = [{"outpoint": "tx1:0", "amount_sat": 20000000, "address": "bc1qtest"}]
        destinations = [{"id": "node_non_wumbo", "amount": 1000000}]
        feerate_per_kw = 1000

        # Peer does NOT support wumbo (features without bit 18 or 19)
        non_wumbo_features = "020000"
        with self.assertRaises(ValueError) as ctx:
            calculate_exact_allocation(
                destinations,
                feerate_per_kw,
                utxos,
                change_to_idx=0,
                peer_features_hex=non_wumbo_features
            )
        err = str(ctx.exception).lower()
        self.assertIn("non-wumbo limit", err)
        self.assertIn("16,777,215", err)

    def test_wumbo_limit_allowed_when_wumbo_supported(self):
        """Case 1: Channel > 16.7M sats with Wumbo support succeeds."""
        utxos = [{"outpoint": "tx1:0", "amount_sat": 20000000, "address": "bc1qtest"}]
        destinations = [{"id": "node_wumbo", "amount": 1000000}]
        feerate_per_kw = 1000

        # Peer supports wumbo (bit 19 set: 0x080000)
        wumbo_features = "080000"
        res = calculate_exact_allocation(
            destinations,
            feerate_per_kw,
            utxos,
            change_to_idx=0,
            peer_features_hex=wumbo_features
        )
        self.assertGreater(res["exact_target_amount"], MAX_NON_WUMBO_SAT)

    def test_user_max_amount_limit_error(self):
        """Case 2: Resulting amount exceeds user-configured max_amount."""
        utxos = [{"outpoint": "tx1:0", "amount_sat": 5000000, "address": "bc1qtest"}]
        destinations = [
            {"id": "node_capped", "amount": 1000000, "max_amount": 2500000}
        ]
        feerate_per_kw = 1000

        with self.assertRaises(ValueError) as ctx:
            calculate_exact_allocation(destinations, feerate_per_kw, utxos, change_to_idx=0)
        err = str(ctx.exception).lower()
        self.assertIn("exceeds the configured 'max_amount'", err)

    def test_dust_limit_error(self):
        """Case 3: Resulting amount is below 546 sats."""
        utxos = [{"outpoint": "tx1:0", "amount_sat": 200000, "address": "bc1qtest"}]
        destinations = [
            {"id": "node1", "amount": 10000},
            {"id": "node2", "amount": 199500}
        ]
        feerate_per_kw = 2500
        with self.assertRaises(ValueError) as ctx:
            calculate_exact_allocation(destinations, feerate_per_kw, utxos, change_to_idx=0)
        self.assertIn("dust threshold", str(ctx.exception).lower())

    def test_missing_change_to_raises_error(self):
        params_without_change_to = {
            "destinations": [{"id": "a", "amount": 100000}],
            "feerate": 700
        }
        with self.assertRaises(ValueError) as ctx:
            process_exact_funding(params_without_change_to)
        self.assertIn("Missing required parameter 'change_to'", str(ctx.exception))

    def test_amount_all_raises_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            parse_sat_amount("all")
        self.assertIn("Destination amount cannot be 'all'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
