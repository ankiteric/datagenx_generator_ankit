#!/usr/bin/env python3
import os
import subprocess
import shutil

# ---------------- CONFIG ----------------
input_dir = "/Users/sreeharshar/work/db/datagenx/mysql-py/dbgen_output_20251224_153546"          # folder containing your .dbgen files
dbgen_binary = "/Users/sreeharshar/work/db/datagenx/code/dbgen/target/release/dbgen"  # path to your dbgen binary
temp_out_dir = "dbgen_tmp_out"      # temporary output folder
files_count = "1"
rows_per_file = "100"
rows_count = "6"
# ---------------------------------------

# Ensure temp output directory exists
os.makedirs(temp_out_dir, exist_ok=True)

# Loop through all .dbgen files
for filename in os.listdir(input_dir):
    if filename.endswith(".dbgen"):
        template_path = os.path.join(input_dir, filename)
        print(f"⚙️ Processing file: {filename}")

        # Run dbgen command
        cmd = [
            dbgen_binary,
            "--out-dir", temp_out_dir,
            "--files-count", files_count,
            "--rows-per-file", rows_per_file,
            "--rows-count", rows_count,
            "--template", template_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            print(f"✅ Completed: {filename}")
        else:
            print(f"❌ Failed: {filename}")
            print(result.stderr)

print("\n🎉 All files processed.")
