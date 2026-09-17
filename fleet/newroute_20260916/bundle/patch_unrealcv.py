#!/usr/bin/env python3
"""Idempotently widen UnrealCV's annotation colour map and stop it indexing past the end.

UnrealCV gives every annotatable actor a colour from a fixed 32^3 = 32768 entry map and indexes it
without a bounds check, so a level with more actors than that aborts the editor at BeginPlay:

    LogUnrealCV: Error: Object index 32768 is out of the color map boundary [0, 32768]
    Assertion failed: (Index >= 0) & (Index < ArrayNum)

Dubai_Downtown has 147393. Two changes: 64 per channel (262144 entries) and wrap instead of
indexing past the end, so no level can crash the editor here again. Widening the map only affects
how unique mask colours are; RGB and depth capture do not use it.

The file ships with CRLF line endings; this rewrites bytes so they are preserved.
Exit 0 = patched now or already patched, 2 = the source did not look as expected.
"""
import sys
from pathlib import Path

src = Path(sys.argv[1])
b = src.read_bytes()
OLD_N = b"int NumPerChannel = 32;"
NEW_N = b"int NumPerChannel = 64;"
OLD_RET = b"\treturn ColorMap[ObjectIndex];"
NEW_RET = (b"\t// Patched: wrapping keeps a scene with more actors than the colour map from\r\n"
           b"\t// asserting in TArray::operator[] and taking the editor down at BeginPlay.\r\n"
           b"\tif (ColorMap.Num() == 0) { return FColor(0, 0, 0, 255); }\r\n"
           b"\tint32 SafeIndex = ObjectIndex % ColorMap.Num();\r\n"
           b"\tif (SafeIndex < 0) { SafeIndex += ColorMap.Num(); }\r\n"
           b"\treturn ColorMap[SafeIndex];")

done_n = NEW_N in b
done_ret = b"SafeIndex" in b
if done_n and done_ret:
    print("already patched"); sys.exit(0)
if not done_n:
    if b.count(OLD_N) != 1:
        print(f"FATAL: expected exactly one {OLD_N!r}, found {b.count(OLD_N)}"); sys.exit(2)
    b = b.replace(OLD_N, NEW_N)
if not done_ret:
    if b.count(OLD_RET) != 1:
        print(f"FATAL: expected exactly one {OLD_RET!r}, found {b.count(OLD_RET)}"); sys.exit(2)
    b = b.replace(OLD_RET, NEW_RET)
src.write_bytes(b)
print("patched: NumPerChannel 32 -> 64, ColorMap index wrapped")
