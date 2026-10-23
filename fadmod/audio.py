"""Simple pygame-based audio manager."""
import math

import numpy as np
import pygame

class Audio:
    def __init__(self):
        self.ok = False
        self.volume = 0.25
        self.shot = None
        self.stereo = False
        try:
            try:
                pygame.mixer.init(22050, -16, 2, 512)  # stereo, so sounds can be panned left / right
                self.stereo = True
            except Exception:
                pygame.mixer.init(22050, -16, 1, 512)
            pygame.mixer.set_num_channels(24)
            n = 6000
            i = np.arange(n)
            d = (np.random.uniform(-1, 1, n) * np.exp(-i / 900) * 32767).astype(np.int16)
            if self.stereo:
                d = np.repeat(d, 2)  # same noise burst in both ears
            self.shot = pygame.mixer.Sound(buffer=d.tobytes())
            self.ok = True
        except Exception as ex:
            print("Audio disabled:", ex)

    def play(self, gain, snd=None, pan=0.0):
        """pan: -1 = fully left, 0 = centre, +1 = fully right (equal-power)."""
        if not self.ok:
            return
        s = snd or self.shot
        v = max(0.0, min(1.0, gain * self.volume))
        try:
            ch = s.play()
            if ch:
                if self.stereo and pan:
                    a = (max(-1.0, min(1.0, pan)) + 1) * math.pi / 4
                    ch.set_volume(v * min(1.0, math.cos(a) * 1.4142), v * min(1.0, math.sin(a) * 1.4142))
                else:
                    ch.set_volume(v)
        except Exception:
            pass

