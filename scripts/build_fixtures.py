"""Build byte-stable synthetic ZIPs. Never fetch production inputs."""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "tests/fixtures/p0"


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    for batch, empty in [("115q1", False), ("101q1", True)]:
        with zipfile.ZipFile(ROOT / f"{batch}_lvr_landcsv.zip", "w") as archive:
            members = {"manifest.csv": "synthetic,fixture\n", "build.ttt": "synthetic\n"} if empty else {}
            if not empty:
                for category in "abc":
                    text = io.StringIO(newline="")
                    csv.writer(text).writerows([
                        ["土地位置建物門牌", "交易年月日", "總價元", "synthetic_unknown"],
                        ["Address", "TransactionDate", "TotalPrice", "Unknown"],
                        ["臺北市中正區測試路10號之1", "1150102", "100000", "preserve"]])
                    members[f"a_lvr_land_{category}.csv"] = text.getvalue()
            for name, contents in sorted(members.items()):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, contents.encode("utf-8-sig"))
    (ROOT / "addresses.csv").write_text("FULL_ADDR,COUNTY,TOWN,VILLAGE,NEIGHBORHOOD,ROAD,SECTION,LANE,ALLEY,SUB_ALLEY,TONG,NUMBER,X,Y\n金門縣金城鎮測試路10號,09020,09020010,,,測試路,,,,,,10號,118.3,24.4\n", encoding="utf-8")


if __name__ == "__main__":
    main()
