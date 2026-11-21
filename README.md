# Billions for Mech Royale

An original bot for the Builders Buildathon Mech Royale track. It uses the official Python protocol wrapper and reads the arena map supplied by the scorer. The bot remembers nearby weapon pickups, seeks cover while recovering, and weighs closing-zone travel against loot and combat. When it observes opponents taking damage from each other, it favors safe cube pickups and avoids crossfire.

Use the official [Mech Royale kit](https://www.joinbuilders.org/buildathon/tracks/builders/files/mechfight-kit.tar.gz) to run the checker. From this repository in a Unix shell, set KIT to the extracted kit directory and run:

```sh
docker build --platform linux/amd64 -t billions-mech .
python3 "$KIT/check/check.py" --map-dir /arena -- \
  docker run --rm -i --network none --cpus 1 --memory 512m --memory-swap 512m \
  --pids-limit 128 --cap-drop ALL --security-opt no-new-privileges \
  --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  -v "$KIT/arena:/arena:ro" billions-mech
```

The checker replays recorded observations. Practice matches on Builders measure placement against the house bots.

The [pinned public proxy simulator](https://github.com/LanceHuang245/mech-royale/tree/6bdf03b2c7031f333a6d37836ecee1b94d3f9127/tools) was used for paired development runs and mixed free-for-alls. Its model approximates the match rules, so its placement results apply to those local matches.
