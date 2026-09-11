# CLN Exact Channel Funding Plugin (`exactmultifundchannel`)

A lightweight, zero-dependency Core Lightning (CLN) plugin that opens multiple Lightning channels using `multifundchannel` without leaving any change output back to your wallet.

---

## Overview & Interface

`exactmultifundchannel` mirrors the exact interface of CLN's `multifundchannel`, adding a **mandatory** `change_to` argument to specify which destination channel absorbs all leftover change satoshis.

Any existing, optional, or future arguments supported by `multifundchannel` are automatically forwarded without hardcoding.

```bash
exactmultifundchannel destinations [feerate] [minconf] [utxos] [minchannels] [commitment_feerate] ... change_to=index
```

### Example

```bash
lightning-cli -k exactmultifundchannel \
  feerate=700 \
  destinations='[{"id":"aaaaa", "amount":1000000},{"id":"bbbb", "amount":2000000}]' \
  utxos='["xxxx:0","yyyy:1"]' \
  change_to=1
```

In this example:
- `change_to=1` (mandatory) selects the channel at index `1` (node `"bbbb"`).
- Node `"aaaaa"` receives its exact requested `1,000,000` sats.
- The exact non-change fee is calculated for the transaction.
- All remaining satoshis from the inputs are added into node `"bbbb"`:
  $$\text{Amount}(\text{"bbbb"}) = 2,000,000 + \text{Change}$$
- `multifundchannel` is called with the adjusted destinations and all other arguments passed through.
- **Change outputs on the funding transaction: 0.**

---

## Safety & Limit Protections (No Miner Leaks)

If for any reason all change cannot be safely added to `destinations[change_to]`, the plugin **aborts immediately with a descriptive error**. It **never** allows your excess satoshis to silently turn into a miner fee bonus.

The plugin detects and prevents:
1. **Non-Wumbo Limit (16,777,215 sats / 0.16777215 BTC)**:
   If the resulting channel size exceeds $16,777,215$ sats, the plugin queries the peer's negotiated feature bits (BOLT #9 bits 18/19: `option_support_large_channel`). If the peer does not support Wumbo, the command halts:
   ```text
   Cannot add change to destination 1 ('bbbb'): resulting channel amount (18,500,000 sats) exceeds the non-Wumbo limit of 16,777,215 sats (0.16777215 BTC) and peer does not support 'option_support_large_channel' (wumbo). Aborting so you can select a wumbo-enabled destination for change_to or provide fewer input funds.
   ```
2. **Channel Limits & User `max_amount`**:
   - You can optionally specify a `max_amount` on any destination (e.g. `{"id": "bbbb", "amount": 2000000, "max_amount": 2500000}`). If the change would cause the channel to exceed `max_amount`, it aborts with an explanatory error.
   - If a peer rejects the channel size during handshake, the plugin traps the rejection and explains why.
3. **Dust Limit (< 546 sats)**:
   If available input funds are insufficient to cover other channels and miner fees, leaving less than 546 sats for the `change_to` channel, the plugin aborts.

$$\sum \text{Inputs} - \sum \text{Outputs} = \text{Fee}_{\text{exact}}$$
$$\text{Change} = 0 \text{ satoshis}$$

---

## Installation

### Prerequisites
- Python 3.8+ (standard library only; **no pip dependencies required**).
- Core Lightning (CLN) v0.10.0 or later.

### Load Dynamically
```bash
lightning-cli plugin start /path/to/exactmultifundchannel.py
```

### Or Load Automatically at Startup
Add to your `~/.lightning/config`:
```text
plugin=/path/to/exactmultifundchannel.py
```

---

## RPC Commands

### 1. `exactmultifundchannel`
Executes `multifundchannel` with zero change outputs.

#### Parameters:
- `destinations` *(array, required)*: List of destination objects (or JSON strings).
  - Optional `max_amount`: Maximum satoshis you allow this channel to receive.
- `change_to` *(integer, required)*: Zero-based index in `destinations` of the channel to absorb all change satoshis. **Mandatory with no default**.
- `feerate` *(string/number, optional)*: Fee rate (e.g. `"normal"`, `"urgent"`, `700`, `"2500perkw"`). Defaults to `"opening"`.
- `utxos` *(array of strings, optional)*: Specific `txid:vout` outpoints to spend.
- `minconf`, `minchannels`, `commitment_feerate`, etc.: Any standard or future `multifundchannel` argument.

---

### 2. `calculate_exact_funding` (Dry Run)
Previews the exact transaction weights, fees, and satoshi allocations without broadcasting or opening channels.

```bash
lightning-cli -k calculate_exact_funding \
  feerate=700 \
  destinations='[{"id":"aaaaa", "amount":1000000},{"id":"bbbb", "amount":2000000}]' \
  utxos='["xxxx:0","yyyy:1"]' \
  change_to=1
```

---

## Running Unit Tests

```bash
python3 -m unittest discover tests
```

---

## License

MIT License. Copyright (c) 2026 btweenthebars.
