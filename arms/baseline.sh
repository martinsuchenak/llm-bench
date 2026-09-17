#!/bin/sh
# Baseline arm: the agent gets the plain repository overlay - ordinary
# grep/read competence, no code-intelligence tooling installed.
#
# Runs with the overlay MOUNT PATH as working directory before the agent
# starts. Anything this script writes lands in the overlay's upper layer and
# is visible to the agent.
exit 0
