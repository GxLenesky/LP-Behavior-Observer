"""Collect Uniswap v3 WETH/USDT Swap and Mint events from Ethereum mainnet."""

import argparse
import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hexbytes import HexBytes
from web3 import Web3


DEFAULT_RPC_URL = "https://eth-mainnet.public.blastapi.io"
POOL_ADDRESS = Web3.to_checksum_address("0x4e68ccd3e89f51c3074ca5072bbac773960dfa36")
EXPECTED_CHAIN_ID = 1
FINALITY_TAG = "finalized"
INITIAL_BATCH_SIZE = 10
MAX_RETRIES = 8
LOG_RPC_BATCH_COUNT = 100
BLOCK_RPC_BATCH_COUNT = 100
BETWEEN_REQUEST_TYPES_SECONDS = 0.0
ROUND_PAUSE_SECONDS = 1.0

SWAP_SIGNATURE = "Swap(address,address,int256,int256,uint160,uint128,int24)"
MINT_SIGNATURE = "Mint(address,address,int24,int24,uint128,uint256,uint256)"
SWAP_TOPIC = Web3.keccak(text=SWAP_SIGNATURE).hex()
MINT_TOPIC = Web3.keccak(text=MINT_SIGNATURE).hex()

FIELDS = [
    "event_type", "block_number", "block_hash", "transaction_hash",
    "transaction_index", "log_index", "timestamp", "is_initialization",
    "amount0_raw", "amount1_raw", "sqrt_price_x96", "tick", "owner",
    "tick_lower", "tick_upper", "liquidity_added",
]

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
EVENTS_PATH = DATA_DIR / "events.csv"
PARTIAL_PATH = DATA_DIR / "events_partial.csv"
CHECKPOINT_PATH = DATA_DIR / "collection_checkpoint.json"
METADATA_PATH = DATA_DIR / "collection_metadata.json"


def utc_text(timestamp):
    return datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def retry(label, operation, attempts=MAX_RETRIES):
    """Retry short-lived provider errors, then return or raise the last error."""
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            response = getattr(exc, "response", None)
            if response is not None and response.status_code == 400:
                raise
            if attempt == attempts:
                raise RuntimeError(f"{label} failed after {attempts} attempts: {exc}") from exc
            delay = min(10, 2 ** (attempt - 1))
            print(f"  {label} failed ({exc}); retrying in {delay}s")
            time.sleep(delay)


def get_block(w3, identifier):
    return retry(f"get block {identifier}", lambda: w3.eth.get_block(identifier))


def get_timestamp(w3, number, cache):
    number = int(number)
    if number not in cache:
        cache[number] = int(get_block(w3, number)["timestamp"])
    return cache[number]


def first_block_at_or_after(w3, target_timestamp, high_block, cache):
    """Binary search the first block whose timestamp reaches the target."""
    low, high = 0, int(high_block)
    if get_timestamp(w3, high, cache) < target_timestamp:
        raise ValueError("The finalized chain does not yet reach the requested timestamp")
    while low < high:
        middle = (low + high) // 2
        if get_timestamp(w3, middle, cache) < target_timestamp:
            low = middle + 1
        else:
            high = middle
    return low


def signed_topic(topic):
    return int.from_bytes(bytes(topic), byteorder="big", signed=True)


def topic_address(topic):
    return Web3.to_checksum_address("0x" + bytes(topic)[-20:].hex())


def decode_log(w3, log, timestamp, is_initialization=False):
    """Decode one event locally using the official Uniswap v3 event layout."""
    topic0 = log["topics"][0].hex()
    row = {field: "" for field in FIELDS}
    row.update({
        "block_number": str(int(log["blockNumber"])),
        "block_hash": log["blockHash"].hex(),
        "transaction_hash": log["transactionHash"].hex(),
        "transaction_index": str(int(log["transactionIndex"])),
        "log_index": str(int(log["logIndex"])),
        "timestamp": utc_text(timestamp),
        "is_initialization": "true" if is_initialization else "false",
    })
    if topic0.lower() == SWAP_TOPIC.lower():
        amount0, amount1, sqrt_price, _liquidity, tick = w3.codec.decode(
            ["int256", "int256", "uint160", "uint128", "int24"], bytes(log["data"])
        )
        row.update({
            "event_type": "Swap", "amount0_raw": str(amount0),
            "amount1_raw": str(amount1), "sqrt_price_x96": str(sqrt_price),
            "tick": str(tick),
        })
    elif topic0.lower() == MINT_TOPIC.lower():
        _sender, liquidity, amount0, amount1 = w3.codec.decode(
            ["address", "uint128", "uint256", "uint256"], bytes(log["data"])
        )
        row.update({
            "event_type": "Mint", "amount0_raw": str(amount0),
            "amount1_raw": str(amount1), "owner": topic_address(log["topics"][1]),
            "tick_lower": str(signed_topic(log["topics"][2])),
            "tick_upper": str(signed_topic(log["topics"][3])),
            "liquidity_added": str(liquidity),
        })
    else:
        raise ValueError(f"Unexpected event topic: {topic0}")
    return row


def get_logs(w3, start_block, end_block, topics):
    params = {
        "address": POOL_ADDRESS, "fromBlock": int(start_block),
        "toBlock": int(end_block), "topics": [topics],
    }
    return retry(
        f"get logs {start_block:,}-{end_block:,}",
        lambda: w3.eth.get_logs(params),
    )


def execute_rpc_batch(w3, requests, label):
    """Execute a JSON-RPC batch and retry only failed subrequests."""
    results = [None] * len(requests)
    pending = list(enumerate(requests))
    last_errors = []
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            responses = w3.provider.make_batch_request([request for _, request in pending])
            if not isinstance(responses, list):
                responses = [responses] * len(pending)
        except Exception as exc:
            responses = [{"error": str(exc)}] * len(pending)

        failed = []
        last_errors = []
        for (original_index, request), response in zip(pending, responses):
            if "result" in response and response["result"] is not None:
                results[original_index] = response["result"]
            else:
                failed.append((original_index, request))
                last_errors.append(response.get("error", response))
        if not failed:
            return results
        if attempt < MAX_RETRIES:
            delay = min(10, 2 ** (attempt - 1))
            print(f"  {label}: retrying {len(failed)} failed subrequests in {delay}s", flush=True)
            time.sleep(delay)
        pending = failed
    raise RuntimeError(
        f"{label} left {len(pending)} failed subrequests after {MAX_RETRIES} attempts: "
        f"{last_errors[:3]}"
    )


def normalize_raw_log(log):
    """Convert a raw JSON-RPC log to the types used by the local decoder."""
    return {
        "blockNumber": int(log["blockNumber"], 16),
        "blockHash": HexBytes(log["blockHash"]),
        "transactionHash": HexBytes(log["transactionHash"]),
        "transactionIndex": int(log["transactionIndex"], 16),
        "logIndex": int(log["logIndex"], 16),
        "topics": [HexBytes(topic) for topic in log["topics"]],
        "data": HexBytes(log["data"]),
    }


def timestamps_for_logs(w3, logs, cache):
    """Fetch missing event-block timestamps in RPC batches and cache them."""
    missing = sorted({int(log["blockNumber"]) for log in logs} - set(cache))
    for offset in range(0, len(missing), BLOCK_RPC_BATCH_COUNT):
        numbers = missing[offset:offset + BLOCK_RPC_BATCH_COUNT]
        requests = [("eth_getBlockByNumber", [hex(number), False]) for number in numbers]
        blocks = execute_rpc_batch(w3, requests, "batched w3.eth.get_block timestamp queries")
        for number, block in zip(numbers, blocks):
            cache[number] = int(block["timestamp"], 16)
        if offset + BLOCK_RPC_BATCH_COUNT < len(missing):
            time.sleep(0.4)


def append_rows(path, rows):
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def read_rows(path):
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def query_adaptive(w3, block_w3, start_block, end_block, batch_size, timestamp_cache):
    """Get a batch, shrinking it when the endpoint rejects the requested range."""
    size = min(batch_size, end_block - start_block + 1)
    while True:
        batch_end = min(start_block + size - 1, end_block)
        try:
            logs = get_logs(w3, start_block, batch_end, [SWAP_TOPIC, MINT_TOPIC])
        except Exception as exc:
            if size <= 1:
                raise RuntimeError(
                    f"Block {start_block:,} could not be collected; no range was skipped: {exc}"
                ) from exc
            new_size = max(1, size // 2)
            print(f"  Provider rejected/failed range of {size:,} blocks; reducing to {new_size:,}")
            size = new_size
            continue
        timestamps_for_logs(block_w3, logs, timestamp_cache)
        rows = [decode_log(w3, log, timestamp_cache[int(log["blockNumber"])]) for log in logs]
        return batch_end, rows, size


def query_many_small_batches(w3, block_w3, start_block, end_block, size, timestamp_cache):
    """Bundle several provider-limited get_logs calls into one JSON-RPC request."""
    ranges = []
    cursor = start_block
    while cursor <= end_block and len(ranges) < LOG_RPC_BATCH_COUNT:
        batch_end = min(cursor + size - 1, end_block)
        ranges.append((cursor, batch_end))
        cursor = batch_end + 1
    requests = []
    for batch_start, batch_end in ranges:
        params = {
            "address": POOL_ADDRESS, "fromBlock": hex(batch_start), "toBlock": hex(batch_end),
            "topics": [[SWAP_TOPIC, MINT_TOPIC]],
        }
        requests.append(("eth_getLogs", [params]))
    results = execute_rpc_batch(
        w3, requests, f"batched w3.eth.get_logs queries through {ranges[-1][1]:,}"
    )
    logs = [normalize_raw_log(log) for result in results for log in result]
    if BETWEEN_REQUEST_TYPES_SECONDS:
        time.sleep(BETWEEN_REQUEST_TYPES_SECONDS)
    timestamps_for_logs(block_w3, logs, timestamp_cache)
    rows = [decode_log(w3, log, timestamp_cache[int(log["blockNumber"])]) for log in logs]
    return ranges[-1][1], rows, ranges


def find_initial_swap(w3, before_block, timestamp_cache):
    """Search backward for the latest Swap strictly before the sample."""
    search_end = before_block
    size = INITIAL_BATCH_SIZE
    while search_end >= 0:
        search_start = max(0, search_end - size + 1)
        try:
            logs = get_logs(w3, search_start, search_end, [SWAP_TOPIC])
        except Exception:
            if size <= 1:
                raise
            size = max(1, size // 2)
            continue
        if logs:
            latest = max(logs, key=lambda item: (int(item["blockNumber"]), int(item["logIndex"])))
            number = int(latest["blockNumber"])
            return decode_log(w3, latest, get_timestamp(w3, number, timestamp_cache), True)
        if search_start == 0:
            break
        print(f"  No earlier Swap in {search_start:,}-{search_end:,}; searching farther back")
        search_end = search_start - 1
        size = min(size * 2, 100_000)
    raise RuntimeError("No Swap was found before the sample start")


def checkpoint_matches(checkpoint, start_block, end_block):
    return (
        checkpoint.get("pool", "").lower() == POOL_ADDRESS.lower()
        and checkpoint.get("start_block") == start_block
        and checkpoint.get("end_block") == end_block
    )


def coalesce_ranges(ranges):
    """Keep the checkpoint compact by merging adjacent completed ranges."""
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def write_metadata(base, status, checkpoint=None, error=None):
    result = dict(base)
    result["collection_status"] = status
    completed_end = checkpoint.get("next_block", base["start_block"]) - 1 if checkpoint else None
    result["completed_coverage"] = {
        "start_block": base["start_block"] if completed_end is not None and completed_end >= base["start_block"] else None,
        "end_block": completed_end if completed_end is not None and completed_end >= base["start_block"] else None,
        "completed_through_timestamp": checkpoint.get("completed_through_timestamp") if checkpoint else None,
        "completed_ranges": coalesce_ranges(checkpoint.get("completed_ranges", [])) if checkpoint else [],
    }
    if error:
        result["error"] = str(error)
    save_json(METADATA_PATH, result)


def collect(days):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rpc_url = os.environ.get("RPC_URL", DEFAULT_RPC_URL)
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 45}))
    if not retry("RPC connectivity check", w3.is_connected):
        raise RuntimeError("Could not connect to the configured Ethereum RPC endpoint")
    chain_id = retry("chain ID query", lambda: w3.eth.chain_id)
    if chain_id != EXPECTED_CHAIN_ID:
        raise RuntimeError(f"Expected Ethereum mainnet chain ID 1, received {chain_id}")

    block_rpc_url = os.environ.get("BLOCK_RPC_URL", rpc_url)
    block_w3 = w3
    if block_rpc_url != rpc_url:
        block_w3 = Web3(Web3.HTTPProvider(block_rpc_url, request_kwargs={"timeout": 60}))
        if not retry("block RPC connectivity check", block_w3.is_connected):
            raise RuntimeError("Could not connect to BLOCK_RPC_URL")
        block_chain_id = retry("block RPC chain ID query", lambda: block_w3.eth.chain_id)
        if block_chain_id != EXPECTED_CHAIN_ID:
            raise RuntimeError(f"BLOCK_RPC_URL is not Ethereum mainnet: chain ID {block_chain_id}")

    timestamp_cache = {}
    finalized = get_block(w3, FINALITY_TAG)
    finalized_number = int(finalized["number"])
    finalized_timestamp = int(finalized["timestamp"])
    timestamp_cache[finalized_number] = finalized_timestamp
    last_complete_date = datetime.fromtimestamp(finalized_timestamp, timezone.utc).date() - timedelta(days=1)
    first_date = last_complete_date - timedelta(days=days - 1)
    start_time = datetime.combine(first_date, datetime.min.time(), timezone.utc)
    end_time = datetime.combine(last_complete_date + timedelta(days=1), datetime.min.time(), timezone.utc)

    print(f"Connected to Ethereum mainnet (chain ID {chain_id})")
    print(f"Finalized ceiling: block {finalized_number:,} at {utc_text(finalized_timestamp)}")
    print(f"Collecting {first_date} through {last_complete_date} (complete UTC days)")

    start_block = first_block_at_or_after(w3, int(start_time.timestamp()), finalized_number, timestamp_cache)
    end_block = first_block_at_or_after(w3, int(end_time.timestamp()), finalized_number, timestamp_cache) - 1
    metadata_base = {
        "pool": POOL_ADDRESS, "network": "Ethereum mainnet", "chain_id": chain_id,
        "requested_days": days, "actual_start_date": first_date.isoformat(),
        "actual_end_date": last_complete_date.isoformat(),
        "start_timestamp_utc": utc_text(start_time.timestamp()),
        "end_timestamp_utc_exclusive": utc_text(end_time.timestamp()),
        "start_block": start_block, "end_block": end_block,
        "finalized_ceiling_block": finalized_number,
        "finalized_ceiling_timestamp": utc_text(finalized_timestamp),
        "rpc_source": "RPC_URL environment override" if "RPC_URL" in os.environ else "default public endpoint",
        "block_rpc_source": "BLOCK_RPC_URL environment override" if "BLOCK_RPC_URL" in os.environ else "same as log RPC",
        "event_signatures": {"Swap": SWAP_SIGNATURE, "Mint": MINT_SIGNATURE},
    }

    checkpoint = {}
    if CHECKPOINT_PATH.exists():
        try:
            checkpoint = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            checkpoint = {}
    if checkpoint_matches(checkpoint, start_block, end_block) and PARTIAL_PATH.exists():
        next_block = int(checkpoint["next_block"])
        batch_size = int(checkpoint.get("batch_size", INITIAL_BATCH_SIZE))
        print(f"Resuming at block {next_block:,}")
    else:
        if PARTIAL_PATH.exists():
            PARTIAL_PATH.unlink()
        checkpoint = {
            "pool": POOL_ADDRESS, "start_block": start_block, "end_block": end_block,
            "next_block": start_block, "batch_size": INITIAL_BATCH_SIZE,
            "completed_ranges": [], "status": "in_progress",
        }
        save_json(CHECKPOINT_PATH, checkpoint)
        print("Finding the last Swap before the sample for price initialization", flush=True)
        append_rows(PARTIAL_PATH, [find_initial_swap(w3, start_block - 1, timestamp_cache)])
        next_block, batch_size = start_block, INITIAL_BATCH_SIZE

    write_metadata(metadata_base, "incomplete", checkpoint)
    try:
        while next_block <= end_block:
            if batch_size <= 10:
                batch_end, rows, ranges = query_many_small_batches(
                    w3, block_w3, next_block, end_block, batch_size, timestamp_cache
                )
                used_size = batch_size
            else:
                batch_end, rows, used_size = query_adaptive(
                    w3, block_w3, next_block, end_block, batch_size, timestamp_cache
                )
                ranges = [(next_block, batch_end)]
            append_rows(PARTIAL_PATH, rows)
            checkpoint["completed_ranges"].extend([list(item) for item in ranges])
            checkpoint["completed_ranges"] = coalesce_ranges(checkpoint["completed_ranges"])
            checkpoint["next_block"] = batch_end + 1
            checkpoint["batch_size"] = used_size
            checkpoint["completed_through_timestamp"] = utc_text(
                get_timestamp(w3, batch_end, timestamp_cache)
            )
            save_json(CHECKPOINT_PATH, checkpoint)
            print(f"  Blocks {next_block:,}-{batch_end:,}: {len(rows):,} events", flush=True)
            next_block = batch_end + 1
            batch_size = min(INITIAL_BATCH_SIZE, used_size)
            time.sleep(ROUND_PAUSE_SECONDS)

        print("Confirming the last Swap before the sample for price initialization")
        initial_swap = find_initial_swap(w3, start_block - 1, timestamp_cache)
        rows = read_rows(PARTIAL_PATH)
        rows.append(initial_swap)
        key = lambda row: (int(row["block_number"]), int(row["transaction_index"]), int(row["log_index"]))
        rows.sort(key=key)
        unique = {(row["transaction_hash"].lower(), row["log_index"]): row for row in rows}
        rows = sorted(unique.values(), key=key)
        with EVENTS_PATH.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        checkpoint["status"] = "complete"
        checkpoint["next_block"] = end_block + 1
        checkpoint["completed_ranges"] = coalesce_ranges(checkpoint["completed_ranges"])
        checkpoint["completed_through_timestamp"] = utc_text(
            get_timestamp(w3, end_block, timestamp_cache)
        )
        save_json(CHECKPOINT_PATH, checkpoint)
        write_metadata(metadata_base, "complete", checkpoint)
        sample_rows = [row for row in rows if row["is_initialization"] != "true"]
        swaps = sum(row["event_type"] == "Swap" for row in sample_rows)
        mints = sum(row["event_type"] == "Mint" for row in sample_rows)
        print(f"Saved {swaps:,} Swaps and {mints:,} Mints to {EVENTS_PATH}")
    except (Exception, KeyboardInterrupt) as exc:
        write_metadata(metadata_base, "incomplete", checkpoint, error=exc)
        print(f"Collection stopped with partial data retained in {PARTIAL_PATH}")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="number of latest complete UTC days")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")
    collect(args.days)


if __name__ == "__main__":
    main()
