"""Validación final del pool de perfiles."""
from profile_prepare import prepare_all
from profile_pool import scan_all_profiles, startup_report

report = prepare_all(auto_ocr=False)
print(report.summary())
print("---")
scans = scan_all_profiles()
ready = sum(1 for s in scans if s.ready)
fail = [s for s in scans if not s.ready]
print(f"Total escaneados: {len(scans)} | Listos: {ready} | Fallidos: {len(fail)}")
for s in fail:
    print(f"  FAIL {s.id}: {'; '.join(s.errors)}")
