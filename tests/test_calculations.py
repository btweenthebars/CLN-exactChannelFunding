import unittest
import sys
import os

# Add parent dir to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from exactmultifundchannel import (
    bitcoin_tx_core_weight,
    get_utxo_spend_weight,
    calculate_exact_allocation,
    P2WSH_OUTPUT_WEIGHT,
    DUST_LIMIT_SAT
)

class TestExactChannelFunding(unittest.TestCase):

    def test_core_weight(self):
        # 1 input, 1 output -> (4 + 1 + 1 + 4)*4 + 2 = 42 WU
        self.assertEqual(bitcoin_tx_core_weight(1, 1), 42)
        # 2 inputs, 3 outputs -> (4 + 1 + 1 + 4)*4 + 2 = 42 WU
        self.assertEqual(bitcoin_tx_core_weight(2, 3), 42)
        # Large input count (253 inputs takes 3 bytes varint)
        self.assertEqual(bitcoin_tx_core_weight(253, 1), (4 + 3 + 1 + 4) * 4 + 2)

    def test_utxo_spend_weight(self):
        # Native SegWit (P2WPKH) -> 271 WU
        self.assertEqual(get_utxo_spend_weight("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"), 271)
        self.assertEqual(get_utxo_spend_weight("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"), 271)
        
        # Taproot (P2TR) -> 230 WU
        self.assertEqual(get_utxo_spend_weight("bc1p5d7rx65gnslx2e4vs3pwc2hnwn9wpmratnyw2w9k9d9529sq4faqbh79ed"), 230)
        self.assertEqual(get_utxo_spend_weight("tb1pqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesf3hn0c"), 230)
        
        # Nested SegWit (P2SH) -> 363 WU
        self.assertEqual(get_utxo_spend_weight("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"), 363)
        self.assertEqual(get_utxo_spend_weight("2MzQwSSnBHWHq3431ukysEpTX5wNiFVJhuY"), 363)

    def test_exact_allocation_single_input_three_destinations(self):
        # 1 UTXO of 1,000,000 sats (P2WPKH: 271 WU)
        utxos = [{"outpoint": "tx1:0", "amount_sat": 1000000, "address": "bc1qtest"}]
        destinations = [
            {"id": "node1", "amount": 100000},
            {"id": "node2", "amount": 200000},
            {"id": "node3", "amount": 300000}
        ]
        # Feerate: 2500 perkw (= 10 sat/vB)
        feerate_per_kw = 2500

        # Total weight:
        # Core: 42
        # Inputs: 1 * 271 = 271
        # Outputs: 3 * 172 = 516
        # Total = 42 + 271 + 516 = 829 WU (207.25 vB)
        expected_weight = 42 + 271 + (3 * 172)
        self.assertEqual(expected_weight, 829)

        # Fee: (2500 * 829) // 1000 = 2072 sats
        expected_fee = (2500 * 829) // 1000
        self.assertEqual(expected_fee, 2072)

        # Node1 is excess node:
        # Node1 gets: 1,000,000 - 200,000 (node2) - 300,000 (node3) - 2072 (fee) = 497,928 sats
        expected_node1 = 1000000 - 200000 - 300000 - 2072
        self.assertEqual(expected_node1, 497928)

        res = calculate_exact_allocation(destinations, feerate_per_kw, utxos, "node1")
        self.assertEqual(res["total_weight"], expected_weight)
        self.assertEqual(res["exact_fee"], expected_fee)
        self.assertEqual(res["exact_excess_amount"], expected_node1)
        self.assertEqual(res["total_inputs"], 1000000)

        # Verify zero change on the transaction:
        total_outs = res["exact_excess_amount"] + 200000 + 300000
        self.assertEqual(res["total_inputs"] - total_outs, res["exact_fee"])

    def test_exact_allocation_multi_inputs(self):
        # 2 UTXOs: one P2TR (230) and one P2WPKH (271)
        utxos = [
            {"outpoint": "tx1:0", "amount_sat": 500000, "address": "bc1ptaproot"},
            {"outpoint": "tx2:1", "amount_sat": 600000, "address": "bc1qsegwit"}
        ]
        destinations = [
            {"id": "node_a", "amount": 100000},
            {"id": "node_b", "amount": 400000}
        ]
        feerate_per_kw = 5000  # 20 sat/vB

        res = calculate_exact_allocation(destinations, feerate_per_kw, utxos, "node_a")
        
        # Verify zero change equation
        total_inputs = 1100000
        total_outputs = res["exact_excess_amount"] + 400000
        self.assertEqual(total_inputs - total_outputs, res["exact_fee"])
        self.assertGreater(res["exact_excess_amount"], DUST_LIMIT_SAT)

    def test_dust_limit_exception(self):
        # UTXO barely covers fees and other outputs
        utxos = [{"outpoint": "tx1:0", "amount_sat": 202000, "address": "bc1qsegwit"}]
        destinations = [
            {"id": "node1", "amount": 10000},
            {"id": "node2", "amount": 200000}
        ]
        feerate_per_kw = 2500  # Fee will be ~1500+ sats, remaining for node1 < 500 sats
        with self.assertRaises(ValueError) as ctx:
            calculate_exact_allocation(destinations, feerate_per_kw, utxos, "node1")
        self.assertIn("dust limit", str(ctx.exception).lower())

    def test_missing_excess_node(self):
        utxos = [{"outpoint": "tx1:0", "amount_sat": 1000000, "address": "bc1qsegwit"}]
        destinations = [{"id": "node1", "amount": 100000}]
        with self.assertRaises(ValueError) as ctx:
            calculate_exact_allocation(destinations, 2500, utxos, "non_existent_node")
        self.assertIn("not found", str(ctx.exception).lower())

if __name__ == "__main__":
    unittest.main()
