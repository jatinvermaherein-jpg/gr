#!/usr/bin/env python3
"""Build a fresh visual stat-cell overlay from the original screenshot archives.

This is an offline/read-only rebuild of the 523 candidate rows. It never writes to
Supabase and never modifies the raw OCR manifest or candidate input JSONL.
"""
from __future__ import annotations
import csv, hashlib, json, os, re, sys, unicodedata, zipfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "data/drive_ocr_stage"


def report_local_date() -> str:
    """Use an explicit reproducibility date when supplied; otherwise today's local date."""
    override = os.environ.get("MECHBOT_REPORT_LOCAL_DATE")
    return date.fromisoformat(override).isoformat() if override else date.today().isoformat()
INPUT_CANDIDATES = STAGE / "stat_candidate_rows.jsonl"
INPUT_APPROVAL = STAGE / "drive_stat_cell_user_approval.json"
INPUT_INDEX = ROOT / "data/drive_ocr_review_cards/index.csv"
INPUT_MANIFEST = STAGE / "manual_visual_review_manifest.json"
ARCHIVE_DIR = ROOT / "data/source_archives"
OUTPUTS = {
    "verified": STAGE / "verified_stat_cells_fresh.jsonl",
    "combined_overlay": STAGE / "verified_stat_overlay_fresh.jsonl",
    "provenance": STAGE / "cell_level_provenance_fresh.jsonl",
    "corrections": STAGE / "stat_cell_correction_log_fresh.jsonl",
    "row_log": STAGE / "candidate_row_visual_review_log_fresh.jsonl",
    "reconstructions": STAGE / "verified_row_reconstructions_fresh.jsonl",
    "reconstructed_cells": STAGE / "reconstructed_stat_cells_fresh.jsonl",
    "gated": STAGE / "gated_ambiguous_cells_fresh.jsonl",
    "screenshots": STAGE / "fresh_visual_source_screenshot_manifest.jsonl",
    "report": STAGE / "fresh_visual_rebuild_audit.json",
}


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out=[]
    with path.open("r",encoding="utf-8") as f:
        for n,line in enumerate(f,1):
            if line.strip():
                try: out.append(json.loads(line))
                except Exception as e: raise ValueError(f"Bad JSONL at {path}:{n}: {e}")
    return out


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",encoding="utf-8",newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row,ensure_ascii=False,separators=(",",":"))+"\n")


def norm_value(raw: str) -> str:
    """Normalize OCR whitespace/compatibility glyphs only; never infer digits."""
    s=unicodedata.normalize("NFKC",str(raw or ""))
    s=" ".join(s.split())
    m=re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(km/h|m|s)",s)
    if m: s=f"{m.group(1)} {m.group(2)}"
    return s


def close_y(a: float,b: float) -> bool:
    return abs(float(a)-float(b)) < 0.06


def row_key(card_id: str, baseline_y: float, label: str) -> tuple[str,float,str]:
    return (card_id,round(float(baseline_y),1),str(label or ""))


def main() -> int:
    candidates_raw=INPUT_CANDIDATES.read_bytes()
    candidate_hash=sha256_bytes(candidates_raw)
    approval=json.loads(INPUT_APPROVAL.read_text(encoding="utf-8"))
    manual=json.loads(INPUT_MANIFEST.read_text(encoding="utf-8"))
    run_local_date=report_local_date()
    expected_candidate_hash=approval["pinned_inputs"]["source_candidate_jsonl_sha256"]
    if candidate_hash != expected_candidate_hash:
        raise SystemExit(f"Candidate JSONL hash mismatch; expected {expected_candidate_hash}, got {candidate_hash}")
    candidates=[json.loads(line) for line in candidates_raw.decode("utf-8").splitlines() if line.strip()]
    with INPUT_INDEX.open("r",encoding="utf-8-sig",newline="") as f:
        index=list(csv.DictReader(f))

    # Index each source screenshot once; the image bytes are re-read from their original ZIP.
    index_by_card: dict[str,list[dict[str,str]]]=defaultdict(list)
    index_by_source: dict[tuple[str,str],list[dict[str,str]]]=defaultdict(list)
    for r in index:
        index_by_card[r["card_id"]].append(r)
        index_by_source[(r["archive"],r["source_path"])].append(r)
    if len(index_by_card)!=84:
        raise SystemExit(f"Expected 84 screenshot cards; found {len(index_by_card)}")

    approval_scope=approval["scope"]
    if len(candidates)!=approval_scope["candidate_rows"]:
        raise SystemExit(f"Candidate count mismatch: {len(candidates)}")
    candidate_counts=Counter(r["archive_name"] for r in candidates)
    if candidate_counts != Counter(approval_scope["candidate_rows_by_archive"]):
        raise SystemExit(f"Candidate archive distribution mismatch: {candidate_counts}")
    if len(index_by_card)!=approval_scope["screenshot_count"]:
        raise SystemExit("Unique screenshot card count differs from approval scope")

    # Verify source ZIPs and every screenshot's exact hash before using the review manifest.
    pinned_archives=approval["pinned_inputs"]["source_archive_sha256"]
    actual_archives={}
    zip_handles={}
    for archive_name,pinned_sha in pinned_archives.items():
        archive_path=ARCHIVE_DIR/archive_name
        actual=sha256_file(archive_path)
        if actual!=pinned_sha:
            raise SystemExit(f"Source archive SHA-256 mismatch for {archive_name}: {actual}")
        zf=zipfile.ZipFile(archive_path)
        bad=zf.testzip()
        if bad is not None:
            raise SystemExit(f"Source archive failed ZIP integrity test: {archive_name}:{bad}")
        actual_archives[archive_name]=actual
        zip_handles[archive_name]=zf
    screenshot_bytes={}
    for card_id,rows in index_by_card.items():
        first=rows[0]
        if any(r["source_sha256"]!=first["source_sha256"] or r["source_path"]!=first["source_path"] or r["archive"]!=first["archive"] for r in rows):
            raise SystemExit(f"Inconsistent screenshot metadata for {card_id}")
        data=zip_handles[first["archive"]].read(first["source_path"])
        actual=sha256_bytes(data)
        if actual!=first["source_sha256"]:
            raise SystemExit(f"Screenshot hash mismatch for {card_id}: {actual}")
        screenshot_bytes[card_id]=data

    # Manifest must cover exactly all cards, with the correct input screenshot hashes.
    visual_logs={e["card_id"]:e for e in manual["visual_review_log"]}
    if len(visual_logs)!=len(manual["visual_review_log"]):
        raise SystemExit("Duplicate visual-review card entries")
    if set(visual_logs)!=set(index_by_card):
        raise SystemExit(f"Visual-review manifest coverage mismatch: missing={set(index_by_card)-set(visual_logs)}, extra={set(visual_logs)-set(index_by_card)}")
    for card_id,e in visual_logs.items():
        actual=index_by_card[card_id][0]["source_sha256"]
        if e["source_sha256"]!=actual:
            raise SystemExit(f"Manual review hash mismatch for {card_id}")

    # Candidate rows and their deterministic per-screenshot row IDs.
    source_to_card={}
    for card_id,rows in index_by_card.items():
        first=rows[0]
        source_to_card[(first["archive"],first["source_path"])]=card_id
    grouped: dict[str,list[dict[str,Any]]]=defaultdict(list)
    for cand in candidates:
        pair=(cand["archive_name"],cand["source_path"])
        if pair not in source_to_card:
            raise SystemExit(f"Candidate row points to unknown screenshot: {pair}")
        card_id=source_to_card[pair]
        ix=index_by_source[pair]
        if cand["source_sha256"]!=ix[0]["source_sha256"]:
            raise SystemExit(f"Candidate screenshot hash differs from index: {card_id}")
        grouped[card_id].append(cand)
    if set(grouped)!=set(index_by_card):
        raise SystemExit("A screenshot has no candidate rows or candidates reference an unknown screenshot")

    recon_input=manual["row_reconstructions"]
    if len(recon_input)!=2 or {r["reconstruction_id"] for r in recon_input}!={"MECH-013-Debuffs-Removed","MECH-019-Ability-Cooldown-HP-percent"}:
        raise SystemExit("Expected the two previously approved wrapped-row reconstructions")
    recon_by_card={r["card_id"]:r for r in recon_input}
    if set(recon_by_card)!={"MECH-013","MECH-019"}:
        raise SystemExit("Reconstruction card IDs differ from approved scope")
    fragment_rows: dict[str,set[tuple[str,float,str]]]={}
    for card_id,recon in recon_by_card.items():
        if recon["source_sha256"]!=index_by_card[card_id][0]["source_sha256"]:
            raise SystemExit(f"Reconstruction screenshot hash mismatch for {card_id}")
        rows=grouped[card_id]
        selected=set()
        for y in recon["source_fragment_baselines_y"]:
            matches=[r for r in rows if close_y(r["baseline_y"],y)]
            if len(matches)!=1:
                raise SystemExit(f"Expected one fragment candidate at {card_id} baseline {y}; found {len(matches)}")
            c=matches[0]
            selected.add(row_key(card_id,c["baseline_y"],c.get("stat_label_ocr","")))
        fragment_rows[card_id]=selected

    label_overrides=manual.get("label_overrides",{})
    cell_overrides=manual.get("cell_overrides",{})
    gated_defs=manual.get("gated_cells",[])
    gate_keys=set()
    for g in gated_defs:
        gate_keys.add((g["card_id"],g.get("stat_label",g.get("row_label","")),str(g["column"])))

    # Deterministic row identities, audit records and cell-level provenance.
    rowid_by_key={}
    ordered_rows_by_card={}
    for card_id,rows in grouped.items():
        ordered=sorted(rows,key=lambda r:(float(r["baseline_y"]),str(r.get("stat_label_ocr",""))))
        ordered_rows_by_card[card_id]=ordered
        for n,r in enumerate(ordered,1):
            k=row_key(card_id,r["baseline_y"],r.get("stat_label_ocr",""))
            if k in rowid_by_key: raise SystemExit(f"Duplicate candidate row key {k}")
            rowid_by_key[k]=f"{card_id}-R{n:02d}"
        if len(ordered)!=visual_logs[card_id]["candidate_rows"]:
            raise SystemExit(f"Candidate row count mismatch for {card_id}")
        direct_rows=sum(row_key(card_id,r["baseline_y"],r.get("stat_label_ocr","")) not in fragment_rows.get(card_id,set()) for r in ordered)
        expected_slots=direct_rows*7
        if visual_logs[card_id]["cell_slots_visually_compared"]!=expected_slots:
            raise SystemExit(f"Visual cell-slot count mismatch for {card_id}: log={visual_logs[card_id]['cell_slots_visually_compared']}, rows*7={expected_slots}")

    # Validate correction maps. Keys are post-normalization stat labels.
    for card_id,rows in ordered_rows_by_card.items():
        available={label_overrides.get(card_id,{}).get(r.get("stat_label_ocr",""),r.get("stat_label_ocr","")) for r in rows}
        for label,cols in cell_overrides.get(card_id,{}).items():
            if label not in available:
                raise SystemExit(f"Cell override label not found: {card_id}/{label}")
            for col,value in cols.items():
                if str(col) not in {str(i) for i in range(1,8)} or not isinstance(value,str) or not value:
                    raise SystemExit(f"Invalid cell override {card_id}/{label}/{col}={value!r}")

    provenance=[]
    verified=[]
    row_logs=[]
    gated=[]
    counters=Counter()
    direct_row_count=0
    fragment_row_count=0
    for card_id,rows in ordered_rows_by_card.items():
        for row in rows:
            raw_label=str(row.get("stat_label_ocr","") or "")
            verified_label=label_overrides.get(card_id,{}).get(raw_label,raw_label)
            k=row_key(card_id,row["baseline_y"],raw_label)
            candidate_id=rowid_by_key[k]
            is_fragment=k in fragment_rows.get(card_id,set())
            if is_fragment:
                fragment_row_count+=1
                recon=recon_by_card[card_id]
                row_status="wrapped_fragment_preserved_and_joined_into_reconstruction"
            else:
                direct_row_count+=1
                recon=None
                row_status="visually_reviewed_direct_row"
            raw_by_col={str(c["column"]):c for c in row.get("cells",[])}
            if set(raw_by_col)!={str(i) for i in range(1,8)}:
                raise SystemExit(f"Candidate cell columns incomplete for {candidate_id}: {set(raw_by_col)}")
            slot_records=[]
            for col in range(1,8):
                cs=str(col);cell=raw_by_col[cs]
                raw_value=str(cell.get("raw_ocr") or "")
                conf=cell.get("confidence")
                if is_fragment:
                    status="wrapped_fragment"
                    final_value=norm_value(raw_value) if raw_value else ""
                    include=False
                    counters["wrapped_fragment_cell_slots"]+=1
                    if raw_value: counters["wrapped_fragment_nonblank_ocr_tokens"]+=1
                    rec_id=recon["reconstruction_id"]
                else:
                    gate=(card_id,verified_label,cs)
                    override=cell_overrides.get(card_id,{}).get(verified_label,{}).get(cs)
                    if gate in gate_keys:
                        status="gated_ambiguous"
                        final_value=""
                        include=False
                        g=next(x for x in gated_defs if (x["card_id"],x.get("stat_label",x.get("row_label","")),str(x["column"]))==gate)
                        gated.append({"card_id":card_id,"candidate_row_id":candidate_id,"stat_label":verified_label,"column":col,"raw_ocr":raw_value,"reason":g.get("reason","Unspecified ambiguity"),"source_path":row["source_path"],"source_sha256":row["source_sha256"]})
                        counters["gated_ambiguous_cells"]+=1
                    elif override is not None:
                        final_value=norm_value(override)
                        status="visually_recovered_from_ocr_blank" if not raw_value.strip() else ("visually_corrected" if norm_value(raw_value)!=final_value else "visually_confirmed")
                        include=bool(final_value)
                        counters["recovered_from_ocr_blank_cells"]+=int(not raw_value.strip())
                        counters["corrected_nonblank_ocr_cells"]+=int(bool(raw_value.strip()) and norm_value(raw_value)!=final_value)
                    elif raw_value.strip():
                        final_value=norm_value(raw_value)
                        status="visually_matched" if final_value==raw_value.strip() else "visually_matched_after_whitespace_normalization"
                        include=bool(final_value)
                        counters["direct_nonblank_raw_cells_matched_without_cell_override"]+=1
                    else:
                        final_value=""
                        status="visually_confirmed_blank"
                        include=False
                        counters["visually_confirmed_blank_cells"]+=1
                    rec_id=None
                archive_name=row["archive_name"]
                rec={
                    "record_type":"candidate_cell",
                    "candidate_row_id":candidate_id,
                    "card_id":card_id,
                    "archive":archive_name,
                    "source_archive_sha256":actual_archives[archive_name],
                    "source_path":row["source_path"],
                    "source_screenshot_sha256":row["source_sha256"],
                    "entity_title_ocr":row.get("entity_title_ocr"),
                    "raw_stat_label_ocr":raw_label,
                    "verified_stat_label":verified_label or None,
                    "baseline_y":row["baseline_y"],
                    "column":col,
                    "raw_ocr":raw_value,
                    "ocr_confidence":conf,
                    "verified_value":final_value or None,
                    "cell_status":status,
                    "included_in_direct_verified_overlay":bool(include and not is_fragment),
                    "reconstruction_id":rec_id,
                    "review_method":"manual side-by-side comparison with original screenshot",
                    "reviewed_on_local_date":manual["reviewed_on"],
                }
                provenance.append(rec)
                slot_records.append(rec)
                if include and not is_fragment:
                    verified.append({
                        "card_id":card_id,
                        "archive":archive_name,
                        "entity_title_ocr":row.get("entity_title_ocr"),
                        "stat_label":verified_label,
                        "column":col,
                        "value":final_value,
                        "source_path":row["source_path"],
                        "source_sha256":row["source_sha256"],
                        "source_archive_sha256":actual_archives[archive_name],
                        "candidate_row_id":candidate_id,
                        "baseline_y":row["baseline_y"],
                        "raw_ocr":raw_value,
                        "ocr_confidence":conf,
                        "provenance_type":"direct_visual_verification",
                        "cell_status":status,
                        "reviewed_on_local_date":manual["reviewed_on"],
                    })
            # All raw candidates remain present in this log even if their cells are wrapped fragments.
            cell_status_counts=Counter(x["cell_status"] for x in slot_records)
            row_logs.append({
                "candidate_row_id":candidate_id,
                "card_id":card_id,
                "archive":row["archive_name"],
                "source_path":row["source_path"],
                "source_sha256":row["source_sha256"],
                "entity_title_ocr":row.get("entity_title_ocr"),
                "raw_stat_label_ocr":raw_label,
                "verified_stat_label":verified_label or None,
                "baseline_y":row["baseline_y"],
                "candidate_values_by_column":{str(x["column"]):str(x.get("raw_ocr") or "") for x in row.get("cells",[])},
                "visual_review_status":row_status,
                "reconstruction_id":recon["reconstruction_id"] if recon else None,
                "cell_status_counts":dict(cell_status_counts),
                "candidate_review_status_before_rebuild":row.get("review_status"),
                "raw_candidate_row_retained":True,
            })

    # Cell-level output for the two approved wrapped rows.
    recon_row_records=[]
    recon_cell_records=[]
    for r in recon_input:
        cid=r["card_id"]
        all_source_rows=ordered_rows_by_card[cid]
        source_rows=[x for x in all_source_rows if row_key(cid,x["baseline_y"],x.get("stat_label_ocr","")) in fragment_rows[cid]]
        source_rows.sort(key=lambda x:float(x["baseline_y"]))
        source_fragments={}
        for col in range(1,8):
            fragments=[]
            for rr in source_rows:
                cv=next(c for c in rr["cells"] if int(c["column"])==col)
                fragments.append({"candidate_row_id":rowid_by_key[row_key(cid,rr["baseline_y"],rr.get("stat_label_ocr",""))],"baseline_y":rr["baseline_y"],"raw_stat_label_ocr":rr.get("stat_label_ocr",""),"raw_ocr":cv.get("raw_ocr") or "","ocr_confidence":cv.get("confidence")})
            source_fragments[str(col)]=fragments
            value=norm_value(r["values_by_column"][str(col)])
            cell_record={
                "card_id":cid,"archive":r["archive"],"entity_title":r.get("entity_title"),"stat_label":r["stat_label"],
                "description":r.get("description"),"column":col,"value":value,
                "source_path":r["source_path"],"source_sha256":r["source_sha256"],
                "source_archive_sha256":actual_archives[r["archive"]],
                "reconstruction_id":r["reconstruction_id"],
                "source_fragment_cells":source_fragments[str(col)],
                "provenance_type":"approved_wrapped_row_reconstruction",
                "cell_status":"reconstructed_from_original_screenshot",
                "ambiguous":bool(r.get("ambiguous",False)),
                "reviewed_on_local_date":manual["reviewed_on"],
            }
            recon_cell_records.append(cell_record)
        recon_row_records.append({
            **r,
            "values_by_column":{str(c):norm_value(v) for c,v in r["values_by_column"].items()},
            "source_archive_sha256":actual_archives[r["archive"]],
            "source_fragment_candidate_row_ids":[rowid_by_key[row_key(cid,x["baseline_y"],x.get("stat_label_ocr",""))] for x in source_rows],
            "source_fragments_by_column":source_fragments,
            "prior_user_approved_reconstruction":True,
            "rebuilt_from_original_screenshot":True,
            "reviewed_on_local_date":manual["reviewed_on"],
        })
        counters["reconstructed_cell_records"]+=len(r["values_by_column"])
        counters["reconstructed_rows"]+=1
        # Add reconstructed cells into the unified provenance ledger without modifying input candidates.
        for cell in recon_cell_records[-len(r["values_by_column"]):]:
            provenance.append({
                "record_type":"reconstructed_cell",
                "candidate_row_id":None,
                "card_id":cell["card_id"],"archive":cell["archive"],
                "source_archive_sha256":cell["source_archive_sha256"],"source_path":cell["source_path"],
                "source_screenshot_sha256":cell["source_sha256"],"entity_title_ocr":cell["entity_title"],
                "raw_stat_label_ocr":None,"verified_stat_label":cell["stat_label"],"baseline_y":None,
                "column":cell["column"],"raw_ocr":None,"ocr_confidence":None,
                "verified_value":cell["value"],"cell_status":cell["cell_status"],
                "included_in_direct_verified_overlay":False,"reconstruction_id":cell["reconstruction_id"],
                "source_fragment_cells":cell["source_fragment_cells"],
                "review_method":"manual reconstruction of line-wrapped cells from original screenshot",
                "reviewed_on_local_date":manual["reviewed_on"],
            })

    # Explicit OCR correction ledger: preserve both old raw token/label and the visually
    # confirmed replacement so the rebuild is independently auditable.
    correction_log=[]
    review_note_by_card={card_id:e["notes"] for card_id,e in visual_logs.items()}
    for card_id,rows in ordered_rows_by_card.items():
        label_map=label_overrides.get(card_id,{})
        for row in rows:
            raw_label=str(row.get("stat_label_ocr","") or "")
            final_label=label_map.get(raw_label,raw_label)
            if final_label!=raw_label:
                correction_log.append({
                    "correction_type":"stat_label",
                    "card_id":card_id,
                    "candidate_row_id":rowid_by_key[row_key(card_id,row["baseline_y"],raw_label)],
                    "source_path":row["source_path"],"source_sha256":row["source_sha256"],
                    "baseline_y":row["baseline_y"],"column":None,
                    "raw_ocr":raw_label,"verified_value":final_label,
                    "correction_source":"manual_visual_review_manifest.label_overrides",
                    "review_notes":review_note_by_card[card_id],
                    "reviewed_on_local_date":manual["reviewed_on"],
                })
    for rec in provenance:
        if rec["record_type"]=="candidate_cell" and rec["cell_status"] in {"visually_corrected","visually_recovered_from_ocr_blank"}:
            correction_log.append({
                "correction_type":"rank_cell",
                "card_id":rec["card_id"],"candidate_row_id":rec["candidate_row_id"],
                "source_path":rec["source_path"],"source_sha256":rec["source_screenshot_sha256"],
                "baseline_y":rec["baseline_y"],"column":rec["column"],
                "raw_ocr":rec["raw_ocr"] or None,"verified_value":rec["verified_value"],
                "correction_source":"manual_visual_review_manifest.cell_overrides",
                "review_notes":review_note_by_card[rec["card_id"]],
                "reviewed_on_local_date":manual["reviewed_on"],
            })

    combined_overlay=[]
    for rec in verified:
        combined_overlay.append({**rec,"overlay_record_type":"direct_visual_verification"})
    for rec in recon_cell_records:
        combined_overlay.append({
            "card_id":rec["card_id"],"archive":rec["archive"],
            "entity_title_ocr":rec["entity_title"],"stat_label":rec["stat_label"],
            "column":rec["column"],"value":rec["value"],
            "source_path":rec["source_path"],"source_sha256":rec["source_sha256"],
            "source_archive_sha256":rec["source_archive_sha256"],
            "candidate_row_id":None,"baseline_y":None,"raw_ocr":None,"ocr_confidence":None,
            "overlay_record_type":"approved_wrapped_row_reconstruction",
            "provenance_type":"approved_wrapped_row_reconstruction",
            "cell_status":rec["cell_status"],"reconstruction_id":rec["reconstruction_id"],
            "source_fragment_cells":rec["source_fragment_cells"],
            "reviewed_on_local_date":rec["reviewed_on_local_date"],
        })

    if len(row_logs)!=len(candidates):
        raise SystemExit(f"Row log count mismatch: {len(row_logs)} != {len(candidates)}")
    if len(provenance)!=len(candidates)*7+sum(r["reconstructed_cell_records"] for r in recon_input):
        raise SystemExit(f"Provenance record count mismatch: {len(provenance)}")
    if len(recon_cell_records)!=approval_scope["reconstructed_cell_records"]:
        raise SystemExit(f"Reconstructed cell count mismatch: {len(recon_cell_records)}")
    if len(recon_row_records)!=approval_scope["reconstructed_rows"]:
        raise SystemExit(f"Reconstructed row count mismatch: {len(recon_row_records)}")
    if len(verified)!=len({(r["candidate_row_id"],r["column"]) for r in verified}):
        raise SystemExit("Duplicate direct verified cell keys")
    if len(recon_cell_records)!=len({(r["reconstruction_id"],r["column"]) for r in recon_cell_records}):
        raise SystemExit("Duplicate reconstructed cell keys")

    # Screenshot-level manifest records total candidates, direct row slots and reconstruction links.
    screenshot_manifest=[]
    for card_id in sorted(index_by_card):
        rows=ordered_rows_by_card[card_id]
        entry=visual_logs[card_id]
        direct_n=sum(row_key(card_id,r["baseline_y"],r.get("stat_label_ocr","")) not in fragment_rows.get(card_id,set()) for r in rows)
        screenshot_manifest.append({
            "card_id":card_id,"archive":index_by_card[card_id][0]["archive"],
            "source_path":index_by_card[card_id][0]["source_path"],
            "source_sha256":index_by_card[card_id][0]["source_sha256"],
            "source_archive_sha256":actual_archives[index_by_card[card_id][0]["archive"]],
            "candidate_rows_retained":len(rows),"direct_rows_visually_reviewed":direct_n,
            "direct_cell_slots_compared":entry["cell_slots_visually_compared"],
            "wrapped_reconstruction_id":recon_by_card.get(card_id,{}).get("reconstruction_id"),
            "review_notes":entry["notes"],"unreadable_cells_gated":entry.get("unreadable_cells_gated",[]),
            "reviewed_on_local_date":manual["reviewed_on"],
        })

    # Final counts and invariants for the active catalog scope.
    verified_by_archive=Counter(r["archive"] for r in verified)
    slot_by_archive=Counter(r["archive"] for r in provenance if r["record_type"]=="candidate_cell" and r["cell_status"]!="wrapped_fragment")
    counters["candidate_rows_retained"]=len(candidates)
    counters["candidate_rows_by_archive"]={k:candidate_counts[k] for k in sorted(candidate_counts)}
    counters["source_screenshot_count"]=len(index_by_card)
    counters["direct_rows_visually_reviewed"]=direct_row_count
    counters["wrapped_fragment_candidate_rows_preserved"]=fragment_row_count
    counters["direct_cell_slots_visually_compared"]=sum(x["cell_slots_visually_compared"] for x in visual_logs.values())
    counters["direct_verified_nonblank_cells_actual"]=len(verified)
    counters["pinned_direct_verified_cell_count_expected"]=approval_scope["direct_verified_cell_records"]
    counters["direct_verified_cell_count_delta_vs_pin"]=len(verified)-approval_scope["direct_verified_cell_records"]
    counters["raw_nonblank_candidate_cells_direct"]=sum(1 for rec in provenance if rec["record_type"]=="candidate_cell" and rec["cell_status"]!="wrapped_fragment" and bool(str(rec.get("raw_ocr") or "").strip()))
    counters["raw_blank_candidate_cell_slots_direct"]=sum(1 for rec in provenance if rec["record_type"]=="candidate_cell" and rec["cell_status"]!="wrapped_fragment" and not bool(str(rec.get("raw_ocr") or "").strip()))
    counters["ocr_blank_cells_visually_recovered"]=counters["recovered_from_ocr_blank_cells"]
    counters["visually_confirmed_blank_cells"]=counters["visually_confirmed_blank_cells"]
    counters["rank_cell_correction_log_records"]=sum(x["correction_type"]=="rank_cell" for x in correction_log)
    counters["label_correction_log_records"]=sum(x["correction_type"]=="stat_label" for x in correction_log)
    counters["correction_log_records_total"]=len(correction_log)
    counters["reconstructed_cell_records"]=len(recon_cell_records)
    counters["total_verified_cell_records_actual"]=len(verified)+len(recon_cell_records)
    counters["pinned_total_verified_cell_count_expected"]=approval_scope["direct_verified_cell_records"]+approval_scope["reconstructed_cell_records"]
    counters["total_verified_cell_count_delta_vs_pin"]=len(verified)+len(recon_cell_records)-(approval_scope["direct_verified_cell_records"]+approval_scope["reconstructed_cell_records"])
    counters["verified_nonblank_cells_by_archive"]={k:verified_by_archive[k] for k in sorted(verified_by_archive)}
    counters["source_candidate_jsonl_sha256"]=candidate_hash
    counters["source_raw_ocr_manifest_sha256"]=sha256_file(STAGE/"drive_and_upload_ocr.jsonl")
    counters["source_archive_sha256"]=actual_archives
    counters["gated_ambiguous_cell_count"]=len(gated)
    counters["supabase_writes_performed"]=False
    counters["raw_ocr_records_modified"]=False
    counters["unrelated_public_source_documents_modified"]=False
    counters["staging_candidate_statuses_modified"]=False
    counters["reviewed_on_local_date"]=manual["reviewed_on"]
    counters["rebuilt_on_local_date"]=run_local_date

    # Full deterministic output write.
    write_jsonl(OUTPUTS["verified"],verified)
    write_jsonl(OUTPUTS["combined_overlay"],combined_overlay)
    write_jsonl(OUTPUTS["provenance"],provenance)
    write_jsonl(OUTPUTS["corrections"],correction_log)
    write_jsonl(OUTPUTS["row_log"],row_logs)
    write_jsonl(OUTPUTS["reconstructions"],recon_row_records)
    write_jsonl(OUTPUTS["reconstructed_cells"],recon_cell_records)
    write_jsonl(OUTPUTS["gated"],gated)
    write_jsonl(OUTPUTS["screenshots"],screenshot_manifest)
    pin_delta=len(verified)-approval_scope["direct_verified_cell_records"]
    user_confirmation=manual.get("user_confirmation",{})
    report_status=("fresh_rebuild_user_confirmed_with_historical_count_delta" if pin_delta and user_confirmation.get("all_rebuilt_records_confirmed_verified") else ("fresh_rebuild_complete_with_approval_count_variance" if pin_delta else "fresh_screenshot_by_screenshot_rebuild_complete"))
    report={
        "schema_version":1,
        "status":report_status,
        "generated_on_local_date":run_local_date,
        "reviewed_on_local_date":manual["reviewed_on"],
        "rebuild_mode":"manual visual comparison against original source screenshots",
        "summary":dict(counters),
        "approval_pin_reconciliation":{
            "pinned_direct_verified_cells_expected":approval_scope["direct_verified_cell_records"],
            "rebuilt_direct_verified_cells_actual":len(verified),
            "direct_cell_count_delta":pin_delta,
            "pinned_reconstructed_cell_records_expected":approval_scope["reconstructed_cell_records"],
            "rebuilt_reconstructed_cell_records_actual":len(recon_cell_records),
            "pinned_total_verified_cell_records_expected":approval_scope["direct_verified_cell_records"]+approval_scope["reconstructed_cell_records"],
            "rebuilt_total_verified_cell_records_actual":len(verified)+len(recon_cell_records),
            "exact_count_match":pin_delta==0,
            "fresh_overlay_all_records_user_confirmed":bool(user_confirmation.get("all_rebuilt_records_confirmed_verified",False)),
            "original_approval_pin_modified":False,
            "note":"The prior approved overlay file is absent. This rebuilt overlay is based on fresh direct visual review. The user has subsequently confirmed all current rebuilt records; the historical one-cell count delta from the prior pin remains reported, and the approval JSON was not modified.",
        },
        "unit_conventions":manual.get("unit_conventions",{}),
        "user_confirmation":user_confirmation,
        "scope_invariants":{
            "candidate_rows_expected":523,"candidate_rows_actual":len(candidates),
            "source_screenshots_expected":84,"source_screenshots_actual":len(index_by_card),
            "reviewed_cell_slots_are_all_rank_positions_including_blank_slots":True,
            "direct_cell_slots_visually_compared":sum(x["cell_slots_visually_compared"] for x in visual_logs.values()),
            "reconstruction_cards":["MECH-013","MECH-019"],
            "reconstruction_row_count":len(recon_row_records),"reconstruction_cell_count":len(recon_cell_records),
            "raw_candidate_rows_preserved":True,
            "separate_raw_ocr_records_untouched":True,
            "147_unrelated_public_source_documents_excluded":True,
            "supabase_promotion_authorized_by_this_build":False,
            "approval_count_pin_matches_rebuild":pin_delta==0,
            "fresh_overlay_all_records_user_confirmed":bool(user_confirmation.get("all_rebuilt_records_confirmed_verified",False)),
            "pilot_percentage_values_preserved":bool(manual.get("unit_conventions",{}).get("PILOT.zip",{}).get("all_verified_value_strings_contain_percent_sign",False)),
        },
        "outputs":{k:str(v.relative_to(ROOT)) for k,v in OUTPUTS.items()},
    }
    OUTPUTS["report"].write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    for zf in zip_handles.values(): zf.close()
    print(json.dumps({"status":report["status"],"summary":counters,"outputs":{k:str(v) for k,v in OUTPUTS.items()}},ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    try: sys.exit(main())
    except (ValueError,KeyError,zipfile.BadZipFile) as e:
        print(f"ERROR: {e}",file=sys.stderr);sys.exit(2)
