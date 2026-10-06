"""Measure independent processes across growing synthetic input sizes."""
import json
from pathlib import Path
import subprocess
import sys

from test_p1_conversion import make_source


def test_streaming_memory_and_arrow_batch_budget(tmp_path):
    measurements=[]
    for count in [1000,120000]:
        scope=tmp_path/str(count)
        scope.mkdir()
        raw,manifest=make_source(scope,count=count,categories=("sales",))
        manifest_path=scope/"manifest.json"
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False),encoding="utf-8")
        completed=subprocess.run([sys.executable,"-m","lvr_pipeline","ingest","--raw-dir",str(raw),
            "--manifest",str(manifest_path),"--batch","115q1","--work-dir",str(scope/"work"),"--batch-rows","256"],
            capture_output=True,text=True,check=True)
        result=json.loads(completed.stdout)
        quality=json.loads((Path(result["snapshot"])/"quality.json").read_text(encoding="utf-8"))
        assert quality["input_rows"]==count
        assert quality["max_buffer_rows"]<=256
        measurements.append({"rows":count,"peak_rss_bytes":result["process_peak_rss_bytes"],
                             "elapsed_seconds":result["elapsed_seconds"],"max_buffer_rows":quality["max_buffer_rows"]})
    (tmp_path/"resource-measurements.json").write_text(json.dumps(measurements,indent=2)+"\n",encoding="utf-8")
    assert measurements[1]["peak_rss_bytes"]<=measurements[0]["peak_rss_bytes"]+96*2**20,measurements
    assert measurements[1]["peak_rss_bytes"]<512*2**20,measurements
