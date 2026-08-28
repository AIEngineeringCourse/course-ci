import json


def load_cases(path="golden_set.json"):
    return json.load(open(path))
