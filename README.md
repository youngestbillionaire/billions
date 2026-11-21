# Billions for Mech Royale

An original bot for the Builders Buildathon Mech Royale track. It uses the official Python protocol wrapper and reads the arena map supplied by the scorer. Its policy prioritizes placement, rotates ahead of the closing zone, and chooses when to collect loot, fight, or fall back.

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
