# Legacy Harness Notice

SQF harness workflows are not active.

Use the NCS training harness flow:

```powershell
python scripts\ncs_harness.py collect-training-courses --all-majors --num-of-rows 500
python scripts\ncs_harness.py preprocess-ncs-ontology --atomic-ksa --task-ksa-relations --task-similarity --training-course-links
python scripts\ncs_harness.py recommend-training-for-task --query "인력채용" --limit 5
```

The active checks are:

```powershell
python -m unittest discover -s tests -v
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
python scripts\ncs_harness.py ontology validate
```
