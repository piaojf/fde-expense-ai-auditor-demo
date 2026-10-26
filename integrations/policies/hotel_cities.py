"""Resolve only exact city names explicitly enumerated by a published policy."""

import re


def named_city(matrix, name):
    canonical = name.strip().removesuffix("市")
    matches = []
    for row in matrix["city_classification"]:
        if row["city_tier"] not in {"I", "II"}:
            continue
        names = {n.strip().removesuffix("市") for n in re.split("[、，,]", row["scope"])}
        if canonical in names:
            matches.append(row)
    if len(matches) != 1:
        return None
    return dict(name=canonical, tier=matches[0]["city_tier"],
                level="MUNICIPALITY" if canonical in {"北京", "上海", "天津", "重庆"}
                else "PREFECTURE", aliases=[canonical + "市"], source_row=matches[0])
