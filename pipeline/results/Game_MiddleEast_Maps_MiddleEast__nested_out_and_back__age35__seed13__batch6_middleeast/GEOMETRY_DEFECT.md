# MiddleEast: buildings float above the ground

Measured on every building actor in the level, not a sample:

    building actors with a ground hit   25 of 32
    gap above ground, median            40 cm
    gap above ground, p90               280 cm
    gap above ground, max               700.6 cm   (SM_House_8)
    floating by more than 50 cm         6 of 25
    floating by more than 200 cm        4  (700.6 / 352.2 / 280.0 / 249.2 cm)

Visible from frames 1944-2004 of this episode, where 4.5-5.6% of the frame is geometry with sky
directly underneath it. The median over the whole episode is 0.78%.

This is how the level ships. The capture itself is correct: the camera stands on Landscape_0 at the
requested eye height, the route is collision free (0 sweeps hit, 0 near-plane penetrations, nearest
depth 2.34 m) and all 23 evaluable gates pass. Nothing in the pipeline caused it, and no gate
detects it - the gates check collision, penetration, dead-black regions, material compilation and
pose closure, none of which ask whether the scene's own geometry is plausible.

Two wrong conclusions were reached before this one, both from biased sampling:

  1. An overhead survey was taken from the anchor only, looking in four directions. The buildings
     near the anchor happen to be grounded, so the survey showed nothing wrong.
  2. A per-building ground trace stopped after the first ten actors (an early `break`). Those ten
     are all grounded; the four badly floating ones are further down the list.

Both times the defect was spotted by eye by a person looking at the footage. The human check in
section 14 step 15 is not a formality.
