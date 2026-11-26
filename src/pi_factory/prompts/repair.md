The factory ran the repository checks after your changes and one failed.

Failing check: `$command`
Result: $result

Output (last part):

```
$output
```

Fix the cause of this failure in the working tree, then re-run that check yourself to confirm.
The factory will run all the checks again afterwards. Remember: do not commit and do not push.
If you cannot safely finish without human input, use the same single-line `BLOCKED:` format:
state the concrete reason and ask for what you need. This line is posted on the issue; omit secrets,
private diagnostics, and logs. Otherwise, finish with a short summary of what you fixed.
