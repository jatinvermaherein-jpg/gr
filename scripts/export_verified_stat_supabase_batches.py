#!/usr/bin/env python3
"""Export compact, idempotent SQL batches for the confirmed overlay.

Only the verified overlay, screenshot source manifest, and audit metadata are
included. The separate 90 raw OCR records and 147 unrelated documents are not
read or exported.
"""
from __future__ import annotations
import hashlib, json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
STAGE=ROOT/'data/drive_ocr_stage'
OUT=STAGE/'supabase_promotion_batches'
OVERLAY=STAGE/'verified_stat_overlay_fresh.jsonl'
DIRECT=STAGE/'verified_stat_cells_fresh.jsonl'
SCREENSHOTS=STAGE/'fresh_visual_source_screenshot_manifest.jsonl'
ROW_LOG=STAGE/'candidate_row_visual_review_log_fresh.jsonl'
RECON_ROWS=STAGE/'verified_row_reconstructions_fresh.jsonl'
AUDIT=STAGE/'fresh_visual_rebuild_audit.json'
MANIFEST=STAGE/'manual_visual_review_manifest.json'
CORRECTIONS=STAGE/'stat_cell_correction_log_fresh.jsonl'
CELL_CHUNK_SIZE=600
ROW_CHUNK_SIZE=150
SOURCE_CHUNK_SIZE=28
REVIEW_DATE='2026-11-10'
STATUS_CODES={
 'visually_matched':'m',
 'visually_matched_after_whitespace_normalization':'w',
 'visually_corrected':'c',
 'visually_recovered_from_ocr_blank':'r',
 'visually_confirmed':'v',
 'reconstructed_from_original_screenshot':'x',
}


def sha(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def jlines(path:Path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
def jd(x):return json.dumps(x,ensure_ascii=False,separators=(',',':'))
def q(s):return "'"+str(s).replace("'","''")+"'"

def main():
    overlay=jlines(OVERLAY);screens=jlines(SCREENSHOTS);audit=json.loads(AUDIT.read_text(encoding='utf-8'))
    manual=json.loads(MANIFEST.read_text(encoding='utf-8'));corr=jlines(CORRECTIONS)
    direct=[r for r in overlay if r['overlay_record_type']=='direct_visual_verification']
    recon=[r for r in overlay if r['overlay_record_type']=='approved_wrapped_row_reconstruction']
    pilot=[r for r in direct if r['archive']=='PILOT.zip']
    if (len(overlay),len(direct),len(recon),len(screens),len(corr),len(pilot))!=(3520,3506,14,84,427,504):
        raise SystemExit('Unexpected fresh overlay, provenance, or scope count')
    if any('%' not in r['value'] for r in pilot):raise SystemExit('PILOT percentage suffix check failed')
    if not manual.get('user_confirmation',{}).get('all_rebuilt_records_confirmed_verified'):
        raise SystemExit('Fresh overlay lacks the user confirmation record')

    overlay_sha=sha(OVERLAY);manifest_sha=sha(MANIFEST);correction_sha=sha(STAGE/'stat_cell_correction_log_fresh.jsonl')
    run_id='drive_stat_overlay_20261110_'+overlay_sha[:12]
    OUT.mkdir(parents=True,exist_ok=True)
    archives=audit['summary']['source_archive_sha256']
    user_note='User confirmed all records in the fresh rebuild are verified; PILOT stats, including Mech HP and weapon DMG, are percentages.'
    run_sql=("INSERT INTO mech_bot_app.stat_rebuild_runs (run_id,reviewed_on,candidate_rows,screenshot_count,"
       "direct_cell_slots_compared,direct_verified_cell_count,reconstructed_cell_count,combined_cell_count,"
       "prior_pinned_direct_cell_count,historical_direct_count_delta,candidate_jsonl_sha256,raw_ocr_manifest_sha256,"
       "combined_overlay_sha256,manual_manifest_sha256,correction_log_sha256,source_archive_sha256,user_confirmed,"
       "user_confirmation_note,pilot_unit_convention,pilot_percent_cells,raw_ocr_records_imported,unrelated_public_documents_imported) VALUES ("
       f"{q(run_id)},DATE '{REVIEW_DATE}',523,84,3521,3506,14,3520,3505,1,"
       f"{q(audit['summary']['source_candidate_jsonl_sha256'])},{q(audit['summary']['source_raw_ocr_manifest_sha256'])},"
       f"{q(overlay_sha)},{q(manifest_sha)},{q(correction_sha)},{q(jd(archives))}::jsonb,TRUE,{q(user_note)},"
       "'percentage',504,FALSE,FALSE) ON CONFLICT (run_id) DO NOTHING;\n")
    (OUT/'00_run.sql').write_text(run_sql,encoding='utf-8')

    src_arrays=[]
    for r in screens:
        src_arrays.append([r['card_id'],r['archive'],r['source_path'],r['source_sha256'],r['source_archive_sha256'],
            r['candidate_rows_retained'],r['direct_rows_visually_reviewed'],r['direct_cell_slots_compared'],
            r.get('wrapped_reconstruction_id'),r['review_notes'],r['reviewed_on_local_date']])
    src_insert=("INSERT INTO mech_bot_app.stat_rebuild_sources (rebuild_id,card_id,archive_name,source_path,source_screenshot_sha256,"
      "source_archive_sha256,candidate_rows,direct_rows_visually_reviewed,direct_cell_slots_compared,"
      "wrapped_reconstruction_id,review_notes,reviewed_on)\n"
      f"SELECT {q(run_id)},x->>0,x->>1,x->>2,x->>3,x->>4,(x->>5)::integer,(x->>6)::integer,(x->>7)::integer,"
      "NULLIF(x->>8,'null'),x->>9,(x->>10)::date FROM payload CROSS JOIN LATERAL jsonb_array_elements(payload.j) AS x\n"
      "ON CONFLICT (rebuild_id,card_id) DO NOTHING;\n")
    # Retain a complete convenience SQL file, and split the executable batches below
    # to stay comfortably under connector/API request-size limits.
    full_source_sql=("WITH payload AS (SELECT $overlay_payload$"+jd(src_arrays)+"$overlay_payload$::jsonb AS j)\n"+src_insert)
    (OUT/'01_sources.sql').write_text(full_source_sql,encoding='utf-8')
    source_chunks=[]
    for i in range(0,len(src_arrays),SOURCE_CHUNK_SIZE):
        batch=src_arrays[i:i+SOURCE_CHUNK_SIZE]
        sql=("WITH payload AS (SELECT $overlay_payload$"+jd(batch)+"$overlay_payload$::jsonb AS j)\n"+src_insert)
        path=OUT/f'sources_{i//SOURCE_CHUNK_SIZE+1:02d}.sql';path.write_text(sql,encoding='utf-8')
        source_chunks.append({'file':path.name,'rows':len(batch),'chars':len(sql)})

    direct_rows=jlines(ROW_LOG)
    row_arrays=[]
    for r in direct_rows:
        if r['visual_review_status']!='visually_reviewed_direct_row':continue
        row_arrays.append([r['candidate_row_id'],r['card_id'],r['archive'],r.get('entity_title_ocr'),
            r.get('verified_stat_label'), 'direct',r['candidate_row_id'],None,r.get('baseline_y'),None,None])
    for r in jlines(RECON_ROWS):
        row_arrays.append(['recon:'+r['reconstruction_id'],r['card_id'],r['archive'],r.get('entity_title'),
            r['stat_label'],'reconstructed',None,r['reconstruction_id'],None,r.get('description'),r.get('source_fragment_candidate_row_ids',[])])
    if len(row_arrays)!=505:raise SystemExit(f'Expected 505 effective stat rows, got {len(row_arrays)}')
    row_prefix=("INSERT INTO mech_bot_app.stat_rebuild_rows (rebuild_id,row_key,card_id,archive_name,entity_title,stat_label,"
      "row_type,candidate_row_id,reconstruction_id,baseline_y,description,source_fragment_candidate_row_ids,reviewed_on)\n"
      "SELECT "+q(run_id)+",x->>0,x->>1,x->>2,x->>3,x->>4,x->>5,NULLIF(x->>6,'null'),NULLIF(x->>7,'null'),"
      "NULLIF(x->>8,'null')::numeric,NULLIF(x->>9,'null'),CASE WHEN x->10='null'::jsonb THEN NULL ELSE x->10 END,DATE '"+REVIEW_DATE+"' "
      "FROM payload CROSS JOIN LATERAL jsonb_array_elements(payload.j) AS x\n"
      "ON CONFLICT (rebuild_id,row_key) DO NOTHING;")
    row_chunks=[]
    for i in range(0,len(row_arrays),ROW_CHUNK_SIZE):
        batch=row_arrays[i:i+ROW_CHUNK_SIZE]
        sql=("WITH payload AS (SELECT $overlay_payload$"+jd(batch)+"$overlay_payload$::jsonb AS j)\n"+row_prefix+"\n")
        path=OUT/f'rows_{i//ROW_CHUNK_SIZE+1:02d}.sql';path.write_text(sql,encoding='utf-8')
        row_chunks.append({'file':path.name,'rows':len(batch),'chars':len(sql)})

    cell_arrays=[]
    for r in overlay:
        row_key=r['candidate_row_id'] if r['overlay_record_type']=='direct_visual_verification' else 'recon:'+r['reconstruction_id']
        code=STATUS_CODES.get(r['cell_status'])
        if not code:raise SystemExit('Unknown cell status: '+str(r['cell_status']))
        cell_arrays.append([row_key,int(r['column']),r['value'],code])
    cell_prefix=("INSERT INTO mech_bot_app.verified_stat_cells (rebuild_id,cell_key,row_key,rank_column,value,value_unit,cell_status,user_confirmed,reviewed_on)\n"
      "SELECT "+q(run_id)+",r.row_key||':c'||(x->>1),r.row_key,(x->>1)::smallint,x->>2,"
      "CASE WHEN r.archive_name='PILOT.zip' OR x->>2 LIKE '%\\%%' THEN 'percentage' ELSE 'as_displayed' END,"
      "CASE x->>3 WHEN 'm' THEN 'visually_matched' WHEN 'w' THEN 'visually_matched_after_whitespace_normalization' "
      "WHEN 'c' THEN 'visually_corrected' WHEN 'r' THEN 'visually_recovered_from_ocr_blank' "
      "WHEN 'v' THEN 'visually_confirmed' WHEN 'x' THEN 'reconstructed_from_original_screenshot' END,TRUE,DATE '"+REVIEW_DATE+"' "
      "FROM payload CROSS JOIN LATERAL jsonb_array_elements(payload.j) AS x "
      "JOIN mech_bot_app.stat_rebuild_rows r ON r.rebuild_id="+q(run_id)+" AND r.row_key=x->>0\n"
      "ON CONFLICT (rebuild_id,cell_key) DO NOTHING;")
    cell_chunks=[]
    for i in range(0,len(cell_arrays),CELL_CHUNK_SIZE):
        batch=cell_arrays[i:i+CELL_CHUNK_SIZE]
        sql=("WITH payload AS (SELECT $overlay_payload$"+jd(batch)+"$overlay_payload$::jsonb AS j)\n"+cell_prefix+"\n")
        path=OUT/f'cells_{i//CELL_CHUNK_SIZE+1:02d}.sql';path.write_text(sql,encoding='utf-8')
        cell_chunks.append({'file':path.name,'rows':len(batch),'chars':len(sql)})

    # Remove stale batches left by earlier chunk-size choices so the manifest is
    # the unambiguous source of truth for what may be applied.
    valid_batch_names={x['file'] for x in source_chunks+row_chunks+cell_chunks}
    for pattern in ('sources_*.sql','rows_*.sql','cells_*.sql'):
        for stale in OUT.glob(pattern):
            if stale.name not in valid_batch_names:
                stale.unlink()

    result={'run_id':run_id,'overlay_sha256':overlay_sha,'run_sql':str((OUT/'00_run.sql').relative_to(ROOT)),
        'source_sql':str((OUT/'01_sources.sql').relative_to(ROOT)),'source_rows':len(src_arrays),
        'source_batches':source_chunks,'effective_stat_rows':len(row_arrays),'row_batches':row_chunks,'cell_rows':len(cell_arrays),
        'cell_batches':cell_chunks,'direct_cells':len(direct),'reconstructed_cells':len(recon),
        'pilot_percent_cells':len(pilot),'audit_sha256':sha(AUDIT),'candidate_jsonl_sha256':audit['summary']['source_candidate_jsonl_sha256'],
        'raw_ocr_manifest_sha256':audit['summary']['source_raw_ocr_manifest_sha256'],
        'raw_ocr_records_exported':0,'unrelated_public_documents_exported':0}
    (OUT/'export_manifest.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
