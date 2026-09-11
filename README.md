# CLN Exact Channel Funding Plugin (`exactmultifundchannel`)

A lightweight, zero-dependency Core Lightning (CLN) plugin that opens multiple Lightning payment channels simultaneously using `multifundchannel` **without leaving any change output back to your wallet**. 

All leftover change satoshis are mathematically calculated and absorbed into a designated destination channel specified by `change_to`, saving transaction bytes, eliminating on-chain dust, and preventing unallocated satoshis from leaking to miners.

---

## Table of Contents
1. [The Problem with Default Multifunding](#the-problem-with-default-multifunding)
2. [The Solution: Zero-Change Funding](#the-solution-zero-change-funding)
   - [Why Not Native multifundchannel with "amount": "all"?](#why-not-native-multifundchannel-with-amount-all)
   - [Feature Comparison Table](#feature-comparison-table)
3. [The Exact Calculation (Mathematical Breakdown)](#the-exact-calculation-mathematical-breakdown)
4. [How UTXOs Are Selected & Figured Out](#how-utxos-are-selected--figured-out)
   - [Mode A: Manual UTXOs Specified](#mode-a-manual-utxos-specified)
   - [Mode B: Automatic Coin Selection (No UTXOs Specified)](#mode-b-automatic-coin-selection-no-utxos-specified)
5. [Exceptions & Safeguards (Why Funds Never Leak to Miners)](#exceptions--safeguards-why-funds-never-leak-to-miners)
   - [Exception 1: Non-Wumbo Channel Limit (16,777,215 sats)](#exception-1-non-wumbo-channel-limit-16777215-sats)
   - [Exception 2: User `max_amount` & Peer-Side Size Limits](#exception-2-user-max_amount--peer-side-size-limits)
   - [Exception 3: Dust Limit (< 546 sats)](#exception-3-dust-limit--546-sats)
6. [Command Reference & Manual](#command-reference--manual)
   - [Parameters](#parameters)
   - [CLI Examples](#cli-examples)
   - [Dry-Run Mode (`calculate_exact_funding`)](#dry-run-mode-calculate_exact_funding)
7. [Installation](#installation)
8. [Running Tests](#running-tests)
9. [License](#license)

---

## The Problem with Default Multifunding

When opening multiple channels with CLN's standard `multifundchannel`:
1. **Unwanted Change Outputs**: CLN's coin selector selects UTXOs that exceed the total required. The remainder is sent back to your wallet as an extra change output:
   - Adds **31 to 43 vbytes (124 to 172 weight units)** to the funding transaction.
   - Creates fragmented micro-UTXOs or dust in your on-chain wallet.
   - Requires you to pay another on-chain input fee later when you eventually spend that change UTXO.
2. **The Danger of Native `"amount": "all"`**: While `multifundchannel` supports `"amount": "all"` on one channel, if you do not explicitly pass the `utxos` argument, CLN's internal coin selector sweeps **every single available UTXO in your entire wallet** into that channel. If your wallet has 5 BTC, it sweeps all 5 BTC.

---

## The Solution: Zero-Change Funding

`exactmultifundchannel` solves both problems:
- **Targeted Inputs**: If `utxos` is specified, it uses only those coins. If omitted, it queries CLN's coin selector for just the base channel amounts, preventing whole-wallet sweeps.
- **Exact Allocation**: Calculates the exact transaction fee for a transaction with **zero change outputs**, allocating 100% of the remaining input satoshis into `destinations[change_to]`.
- **Double Savings**: You eliminate the change output bytes, and the fee savings from omitting that output are redirected straight into channel capacity rather than paid to miners.

$$\sum \text{Inputs} - \sum \text{Outputs} = \text{Fee}$$
$$\text{Change Output Count} = 0$$
$$\text{Change Amount} = 0 \text{ satoshis}$$

### Why Not Native `multifundchannel` with `"amount": "all"`?

Core Lightning's native `multifundchannel` does support setting `"amount": "all"` on a single destination. However, relying on native `"amount": "all"` has major risks and limitations:

#### 1. In Automatic Mode (No `utxos` specified): The Whole-Wallet Sweep Trap
If you omit `utxos`, native `multifundchannel` asks CLN's wallet for `"satoshi": "all"`. In Core Lightning ([`wallet/reservation.c:L572`](lightning/wallet/reservation.c)), this instructs the coin selector to **sweep every single available confirmed UTXO in your entire wallet** into that one channel. If your node has 10 UTXOs totaling 4 BTC, it will sweep all 4 BTC into that channel!
- **Our Plugin**: Queries CLN's coin selector for **only the base channel amounts** (e.g. 1M + 2M = 3M sats). It selects only enough coins to cover the batch, leaving the rest of your wallet untouched, and absorbs just the leftover change from those specific coins into `change_to`.

#### 2. In Manual Mode (`utxos` specified): 5 Critical Advantages Over Native
Even if you manually pass `utxos=[...]` (where native `multifundchannel` can produce 0 change), `exactmultifundchannel` provides 5 crucial benefits that native CLN lacks:

1. **Protection Against Silent Non-Wumbo Trimming**:
   If the UTXOs you specify total $> 16,777,215\text{ sats}$ ($0.168\text{ BTC}$) and the `"all"` destination does not support Wumbo (large channels), native CLN ([`multifundchannel.c:L1291`](lightning/plugins/spender/multifundchannel.c)) **silently trims the channel to 16.77M sats, removes `"all"`, and creates a change output anyway**! Your zero-change goal is silently defeated.
   - **Our Plugin**: Checks the peer's BOLT #9 feature bits 18/19 before broadcast and **halts with a clear error**, allowing you to choose a Wumbo-enabled peer or adjust inputs.
2. **Channel Capacity Caps (`max_amount`)**:
   Native CLN has no way to cap an `"all"` channel. If you pass a 5M sat UTXO for a 2M sat base channel, native CLN dumps the entire $\sim 4\text{M sat}$ remainder into that channel.
   - **Our Plugin**: Supports `"max_amount"`. If change pushes the channel above your liquidity ceiling, it halts before broadcasting.
3. **Dry-Run / Preview Mode (`calculate_exact_funding`)**:
   Native CLN provides no way to preview the resulting transaction without broadcasting.
   - **Our Plugin**: Allows full inspection of transaction weight, exact miner fee, and satoshi allocations down to the single satoshi before signing.
4. **Pre-flight Dust Protection**:
   If inputs are slightly too low, native CLN can fail halfway through protocol handshakes.
   - **Our Plugin**: Verifies that the remaining satoshis satisfy the Bitcoin dust threshold ($546\text{ sats}$) mathematically before initiating handshakes.
5. **Clean API Ergonomics**:
   Native CLN forces you to mutate your destination JSON object, replacing an integer amount with the string `"all"`.
   - **Our Plugin**: Keeps all channel amounts as clean integers and uses a simple, separate `change_to=index` parameter.

### Feature Comparison Table

| Feature | Native `multifundchannel` (default) | Native `multifundchannel` (`"all"`) | `exactmultifundchannel` (This Plugin) |
| :--- | :---: | :---: | :---: |
| **Zero Change Output** | ❌ (Creates change) | ✅ (Only with `utxos`) | ✅ **Always 0 change** |
| **Auto Coin Selection (No `utxos`)** | ✅ | ❌ **Sweeps entire wallet** | ✅ **Targeted coin selection** |
| **Non-Wumbo Safety** | N/A | ❌ Silently creates change output | ✅ **Aborts with clear error** |
| **Channel Caps (`max_amount`)** | ❌ | ❌ | ✅ **Supported** |
| **Dry-Run Preview** | ❌ | ❌ | ✅ **`calculate_exact_funding`** |
| **Pre-flight Dust Validation** | ❌ | ❌ | ✅ **Validated before handshake** |
| **API Parameter** | N/A | Mutates `"amount": "all"` | Clean `change_to=index` |

---

## The Exact Calculation (Mathematical Breakdown)

To eliminate the change output, the transaction is structured with $M$ inputs, $N$ channel outputs, and **0 change outputs**.

### 1. Sum Total Inputs ($I$)
$$I = \sum_{j=1}^{M} \text{Input}_j \quad (\text{in satoshis})$$

### 2. Transaction Weight ($W$)
In Bitcoin SegWit, weight is measured in Weight Units (WU), where $4\text{ WU} = 1\text{ vbyte}$:
$$W = W_{\text{core}} + W_{\text{inputs}} + W_{\text{outputs}}$$

- **Core Weight ($W_{\text{core}}$)**: [`lightning/bitcoin/tx.c:L849`](lightning/bitcoin/tx.c)
  $$\text{Version (4B)} + \text{Locktime (4B)} + \text{Input Count (1B)} + \text{Output Count (1B)} = 10\text{B} \times 4 = 40\text{ WU}$$
  $$\text{SegWit Marker (1B)} + \text{Flag (1B)} = 2\text{ WU}$$
  $$W_{\text{core}} = 40 + 2 = \mathbf{42\text{ WU}}$$

- **Channel Outputs Weight ($W_{\text{outputs}}$)**: [`lightning/bitcoin/tx.c:L872`](lightning/bitcoin/tx.c)
  Each Lightning funding output (P2WSH or P2TR) has a 34-byte `scriptPubKey`:
  $$\text{Amount (8B)} + \text{Len Varint (1B)} + \text{Script (34B)} = 43\text{B} \times 4 = \mathbf{172\text{ WU}} \quad (43\text{ vbytes})$$
  $$W_{\text{outputs}} = N \times 172\text{ WU}$$

- **Input Spend Weights ($W_{\text{inputs}}$)**: [`lightning/common/utxo.c:L4`](lightning/common/utxo.c)
  Each input outpoint (36B) + script len (1B) + sequence (4B) $= 41\text{B} \times 4 = 164\text{ WU}$, plus witness data:
  - **P2TR (Taproot `bc1p...`):** $164 + 66 = \mathbf{230\text{ WU}} \quad (57.5\text{ vbytes})$
  - **P2WPKH (Native SegWit `bc1q...`):** $164 + 107 = \mathbf{271\text{ WU}} \quad (67.75\text{ vbytes})$
  - **P2SH-P2WPKH (Nested SegWit `3...`):** $164 + 92 + 107 = \mathbf{363\text{ WU}} \quad (90.75\text{ vbytes})$
  $$W_{\text{inputs}} = \sum_{j=1}^{M} W_{\text{input}, j}$$

### 3. Exact Miner Fee ($\text{Fee}$)
CLN feerates are expressed in `perkw` (satoshis per 1000 weight units). Matching CLN's integer division ([`lightning/common/amount.c:L698`](lightning/common/amount.c)):
$$\text{Fee} = \left\lfloor \frac{\text{feerate} \times W}{1000} \right\rfloor \quad (\text{satoshis})$$

### 4. Sum Other Fixed Channels ($A_{\text{fixed}}$)
$$A_{\text{fixed}} = \sum_{i \ne c} \text{amount}_i$$
*(where $c$ is the index designated by `change_to`)*

### 5. Final Allocation for `destinations[change_to]`
$$\text{Amount}(c) = I - A_{\text{fixed}} - \text{Fee}$$
$$\text{Change Absorbed} = \text{Amount}(c) - \text{BaseAmount}(c)$$

### Concrete Example:
- **Inputs**: Two Native SegWit UTXOs of $2,000,000\text{ sats}$ each $\rightarrow I = 4,000,000\text{ sats}$
- **Channels**:
  - Index 0 (`node "aaaaa"`): $1,000,000\text{ sats}$
  - Index 1 (`node "bbbb"`, `change_to=1`): $2,000,000\text{ sats}$ base
- **Feerate**: $700\text{ perkw}$ ($2.8\text{ sat/vB}$)

1. Weight: $W = 42 + (2 \times 271) + (2 \times 172) = \mathbf{928\text{ WU}}$ ($232\text{ vbytes}$).
2. Fee: $\lfloor (700 \times 928) / 1000 \rfloor = \mathbf{649\text{ sats}}$.
3. Allocation to node `"bbbb"`:
   $$\text{Amount} = 4,000,000 - 1,000,000 - 649 = \mathbf{2,999,351\text{ sats}}$$
4. Node `"bbbb"` receives its base $2,000,000 + 999,351\text{ sats}$ of change.
5. Transaction outputs:
   - Output 0: $1,000,000\text{ sats}$ (`aaaaa`)
   - Output 1: $2,999,351\text{ sats}$ (`bbbb`)
   - Output 2: **None (0 change outputs)**
   - Miner Fee: $649\text{ sats}$

---

## How UTXOs Are Selected & Figured Out

### Mode A: Manual UTXOs Specified
When you provide `utxos=["txid:vout", ...]`:
1. The plugin queries `listfunds` to fetch the exact satoshi amounts and address types for those outpoints.
2. It calculates the exact non-change fee and assigns the remainder to `change_to`.
3. It passes your exact `utxos` array directly to `multifundchannel`.

### Mode B: Automatic Coin Selection (No UTXOs Specified)
How do we know which UTXOs CLN would select without draining your wallet?
We do **not** simulate coin selection. Instead, we query CLN's internal coin selector via `fundpsbt`:
1. **Query CLN (`fundpsbt`)**: The plugin calls `fundpsbt` with:
   - `satoshi`: $\sum \text{base channel amounts}$
   - `feerate`: requested feerate
   - `startweight`: $42 + N \times 172$
   - `reserve`: $100$ (temporary lock so CLN reports the chosen coins)
2. **CLN Selects Optimal UTXOs**: Inside CLN ([`lightning/wallet/reservation.c:L542`](lightning/wallet/reservation.c)), `wallet_find_utxo` runs against your node's live wallet database, selecting the exact optimal coins.
3. **CLN Reports Outpoints**: In the JSON response, CLN returns the `reservations` array containing the exact `txid:vout` pairs it selected.
4. **Immediate Unlock (`unreserveinputs`)**: The plugin immediately calls `unreserveinputs(psbt)` to release the temporary lock so the coins are available for spending.
5. **Compute & Execute**: Using the exact outpoints CLN selected, the plugin calculates the zero-change amount and executes `multifundchannel(destinations=..., utxos=selected_utxos)`.

---

## Exceptions & Safeguards (Why Funds Never Leak to Miners)

If all change cannot be safely added to `destinations[change_to]`, the plugin **halts immediately with an informative error**. It **never** broadcasts a transaction that leaves unallocated satoshis as an accidental miner fee bonus.

### Exception 1: Non-Wumbo Channel Limit (16,777,215 sats)
- Under BOLT #2 / BOLT #9, standard channels are capped at $2^{24} - 1 = \mathbf{16,777,215 \text{ sats}}$ ($\approx 0.168 \text{ BTC}$) unless large channels (Wumbo) are supported.
- If `amount + change > 16,777,215 \text{ sats}`, the plugin queries the peer's feature bitfield (`listpeers` / `listnodes`) for BOLT #9 feature bit 18 (compulsory) or bit 19 (optional) `option_support_large_channel`.
- If the peer does **not** support Wumbo, the command aborts:
  ```text
  Cannot add change to destination 1 ('bbbb'): resulting channel amount (18,500,000 sats) exceeds the non-Wumbo limit of 16,777,215 sats (0.16777215 BTC) and peer does not support 'option_support_large_channel' (wumbo). Aborting so you can select a wumbo-enabled destination for change_to or provide fewer input funds.
  ```

### Exception 2: User `max_amount` & Peer-Side Size Limits
- **User Cap (`max_amount`)**: You can set an optional `max_amount` on any destination (e.g. `{"id": "bbbb", "amount": 2000000, "max_amount": 2500000}`). If the change pushes the channel above `max_amount`, the plugin halts:
  ```text
  Cannot add change to destination 1 ('bbbb'): resulting channel amount (2,999,351 sats) exceeds the configured 'max_amount' of 2,500,000 sats by 499,351 sats. Aborting so you can select an alternative destination for change_to, adjust inputs, or increase max_amount.
  ```
- **Peer Rejection**: If the remote peer rejects the channel during handshake due to its local `--max-channel-size`, the error is trapped and reported clearly.

### Exception 3: Dust Limit (< 546 sats)
- Can only occur in Manual Mode (Mode A) if you provide a UTXO that is too small to cover the other channels plus the miner fee.
- If less than 546 sats remains for `change_to`, the command halts:
  ```text
  Cannot add change to destination 0 ('aaaaa'): resulting channel amount (412 sats) is below the Bitcoin dust threshold (546 sats). Total inputs are insufficient to cover other channels and fees.
  ```

---

## Command Reference & Manual

### Syntax
```bash
exactmultifundchannel destinations [feerate] [minconf] [utxos] [minchannels] [commitment_feerate] ... change_to=index
```

### Parameters
- `destinations` *(array, required)*: Array of destination objects (or JSON strings):
  - `id` *(string, required)*: Peer node pubkey (optional `@host:port`).
  - `amount` *(integer/string, required)*: Base funding amount in satoshis, `'sat'`, `'msat'`, or `'btc'`.
  - `max_amount` *(integer/string, optional)*: Upper limit cap for this channel.
  - `announce`, `push_msat`, `close_to`, `mindepth`, `reserve` *(optional)*.
- `change_to` *(integer, required)*: Zero-based index in `destinations` of the channel that receives all leftover change. **Mandatory with no default**.
- `feerate` *(string/number, optional)*: Target feerate (`"normal"`, `"urgent"`, `"slow"`, `700`, `"2500perkw"`). Defaults to `"opening"`.
- `utxos` *(array of strings, optional)*: Specific `["txid:vout", ...]` outpoints to spend. If omitted, automatic targeted coin selection is used.
- `minconf`, `minchannels`, `commitment_feerate`, etc.: **Any other current or future argument** supported by `multifundchannel` is passed through automatically without hardcoding.

### CLI Examples

#### 1. Automatic Coin Selection with Keyword Arguments (`-k`)
```bash
lightning-cli -k exactmultifundchannel \
  feerate="normal" \
  destinations='[{"id":"02abc...","amount":1000000},{"id":"03def...","amount":2000000}]' \
  change_to=1
```

#### 2. Explicit UTXOs
```bash
lightning-cli -k exactmultifundchannel \
  feerate=700 \
  destinations='[{"id":"02abc...","amount":1000000},{"id":"03def...","amount":2000000}]' \
  utxos='["7a3f8c...12:0","9b1e4a...56:1"]' \
  change_to=1
```

#### 3. Capping the Channel with `max_amount`
```bash
lightning-cli -k exactmultifundchannel \
  feerate=1000 \
  destinations='[{"id":"02abc...","amount":1000000},{"id":"03def...","amount":2000000,"max_amount":2500000}]' \
  change_to=1
```

### Dry-Run Mode (`calculate_exact_funding`)
To inspect the exact satoshi breakdown, transaction weight, and fees without broadcasting or opening channels:

```bash
lightning-cli -k calculate_exact_funding \
  feerate=700 \
  destinations='[{"id":"02abc...","amount":1000000},{"id":"03def...","amount":2000000}]' \
  utxos='["xxxx:0","yyyy:1"]' \
  change_to=1
```

#### Sample Output:
```json
{
  "dry_run": true,
  "summary": {
    "change_to_index": 1,
    "change_to_node_id": "03def...",
    "original_amount_sat": 2000000,
    "change_absorbed_sat": 999351,
    "final_channel_amount_sat": 2999351,
    "exact_fee_sat": 649,
    "total_inputs_sat": 4000000,
    "tx_weight": 928,
    "feerate_per_kw": 700,
    "change_output_count": 0,
    "change_amount_sat": 0,
    "utxos_spent": ["xxxx:0", "yyyy:1"]
  },
  "multifundchannel_params": {
    "destinations": [
      {"id": "02abc...", "amount": 1000000},
      {"id": "03def...", "amount": 2999351}
    ],
    "feerate": "700perkw",
    "utxos": ["xxxx:0", "yyyy:1"]
  }
}
```

---

## Installation

### Prerequisites
- Python 3.8+ (standard library only; **zero external dependencies**).
- Core Lightning (CLN) v0.10.0 or later.

### Option 1: Load Dynamically at Runtime
```bash
lightning-cli plugin start /path/to/CLN-exactChannelFunding/exactmultifundchannel.py
```

### Option 2: Load Automatically on Startup
Add to your `~/.lightning/config`:
```text
plugin=/path/to/CLN-exactChannelFunding/exactmultifundchannel.py
```

---

## Running Tests

The test suite validates all calculations, address weights, bounds checking, Wumbo capability detection, and safety limits:

```bash
python3 -m unittest discover tests
```

Output:
```text
...........
----------------------------------------------------------------------
Ran 11 tests in 0.000s

OK
```

---

## License

MIT License. Copyright (c) 2026 btweenthebars.
