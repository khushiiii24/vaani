Runs from earlier versions, kept because the write-up quotes them. They are NOT in results.csv.

- limiter_v2_no_door_check/burst*: the stand-in burst with the token-bucket limiter but before it
  counted the people already waiting at the door. Those turned away waited a median 6,699 ms for
  their passages-only reply (final version: 2 ms).
- The first real burst (sliding-window limiter, booking per call) is in results.csv as real_burst_v1.
