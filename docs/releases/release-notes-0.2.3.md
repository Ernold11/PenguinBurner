# PenguinBurner 0.2.3 Release Notes

## GitHub Release Notes

PenguinBurner 0.2.3 fixes performance-mode Auto-UV profile generation for full
low-voltage sweeps.

### Auto-UV Performance Profiles

- Performance Auto-OC now saves a complete sweep-shaped V/F profile instead of
  saving only the locally flattened curve around the selected final candidate.
- The final saved curve uses the lowest passed undervolt anchor plus passed
  Auto-OC anchors up to the selected candidate, so scans that reach a manually
  requested low voltage such as `800mV` keep meaningful V/F modifications from
  that lower point.
- The Auto-OC log now reports when the final profile is rebuilt as a
  `performance-sweep` curve and includes the sweep start voltage and anchor
  count for easier issue debugging.

## PyPI Release Summary

PenguinBurner 0.2.3 fixes performance Auto-UV profile saving so full voltage
sweeps preserve V/F curve changes from the lowest passed voltage point.

## Fedora COPR Release Summary

PenguinBurner 0.2.3 fixes performance Auto-UV profile generation for complete
low-voltage V/F sweep profiles.
