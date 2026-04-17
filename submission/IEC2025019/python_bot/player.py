'''
Aces Bot v3 — PokerBots IIITA
==============================
Key fixes vs v1/v2:
  - MC sim count 20-50 max (v1 used 300-500 → clock timeout)
  - round_state.raise_bounds() used directly (v1 had wrong bounds)
  - Aggressive calling vs unknown/random opponents
  - Bounty bonus handled correctly
'''
import random
from itertools import combinations

from skeleton.actions import FoldAction, CallAction, CheckAction, RaiseAction
from skeleton.states import GameState, TerminalState, RoundState
from skeleton.states import NUM_ROUNDS, STARTING_STACK, BIG_BLIND, SMALL_BLIND
from skeleton.bot import Bot
from skeleton.runner import parse_args, run_bot


# ── Card utils ────────────────────────────────────────────────────────────────
RANKS = '23456789TJQKA'
SUITS = 'shdc'
RANK_MAP = {r: i for i, r in enumerate(RANKS)}

def rank(c):  return RANK_MAP[c[0]]
def suit(c):  return SUITS.index(c[1])
def cint(c):  return rank(c) * 4 + suit(c)
def to_ints(lst): return [cint(c) for c in lst]


# ── Fast pure-Python 5-card evaluator ────────────────────────────────────────
def eval5(h):
    rs = sorted([x >> 2 for x in h], reverse=True)
    ss = [x & 3 for x in h]
    fl = len(set(ss)) == 1
    st = False
    if len(set(rs)) == 5:
        if rs[0] - rs[4] == 4:
            st = True
        elif rs == [12, 3, 2, 1, 0]:
            st, rs = True, [3, 2, 1, 0, -1]
    cnt = sorted([(rs.count(r), r) for r in set(rs)], reverse=True)
    g = [c[0] for c in cnt]
    hi = [c[1] for c in cnt]
    if fl and st:      return 8, rs
    if g[0] == 4:      return 7, hi
    if g[:2]==[3,2]:   return 6, hi
    if fl:             return 5, rs
    if st:             return 4, rs
    if g[0] == 3:      return 3, hi
    if g[:2]==[2,2]:   return 2, hi
    if g[0] == 2:      return 1, hi
    return 0, rs

def best7(cards):
    # Use integers: pack rank in upper bits, suit in lower 2
    h = [(c // 4) << 2 | (c % 4) for c in cards]
    return max(eval5(list(c)) for c in combinations(h, 5))


# ── Monte Carlo equity (FAST: default 30 sims) ───────────────────────────────
def mc_equity(mine, board, n=30):
    """Win probability 0-1. n kept LOW (30) to stay within 60s game clock."""
    used = set(mine + board)
    deck = [i for i in range(52) if i not in used]
    need = 5 - len(board)
    wins = 0.0
    for _ in range(n):
        draw   = random.sample(deck, need + 2)
        full_b = board + draw[:need]
        opp    = draw[need:]
        me  = best7(mine + full_b)
        op  = best7(opp  + full_b)
        if   me > op: wins += 1.0
        elif me == op: wins += 0.5
    return wins / n


# ── Preflop hand tier (HU specific) ──────────────────────────────────────────
def pf_tier(hi, lo, suited):
    """1=premium … 5=trash for heads-up"""
    if hi == lo:
        return 1 if hi >= 10 else (2 if hi >= 6 else 3)
    if hi == 12:                                    # Ace-x
        if lo >= 11: return 1                       # AK,AQ
        if lo >= 9:  return 2                       # AT,AJ
        return 3 if suited else 4
    if hi >= 10 and lo >= 9: return 2              # broadway
    if suited and hi - lo <= 2 and lo >= 4: return 3
    if hi - lo <= 1 and hi >= 5: return 4
    return 5


# ── Opponent model ────────────────────────────────────────────────────────────
class Opp:
    def __init__(self):
        self.hands = 0; self.vpip = 0; self.pfr = 0
        self.bets = 0;  self.chks = 0
        self.fcb  = 0;  self.saw  = 0

    def vpip_r(self): return self.vpip / max(self.hands, 1)
    def af(self):     return self.bets  / max(self.chks,  1)
    def fcb_r(self):  return self.fcb   / max(self.saw,   1)

    def fold_eq(self):
        # Default conservative: assume opponent calls often (random bot calls ~75%)
        if self.saw < 8: return 0.30
        return max(0.10, min(0.80, self.fcb_r()))

    def is_passive(self): return self.hands >= 30 and self.af() < 1.0
    def is_loose(self):   return self.hands >= 30 and self.vpip_r() > 0.55


# ── Main Bot ──────────────────────────────────────────────────────────────────
class Player(Bot):

    def __init__(self):
        self.opp = Opp()
        self._bet = False   # did we bet this street?

    def handle_new_round(self, game_state, round_state, active):
        self.opp.hands += 1
        self._bet = False

    def handle_round_over(self, game_state, terminal_state, active):
        pass

    def get_action(self, game_state, round_state, active):
        legal  = round_state.legal_actions()
        street = round_state.street          # 0=preflop 3=flop 4=turn 5=river
        hole   = round_state.hands[active]
        board  = round_state.deck[:street]
        my_pip = round_state.pips[active]
        op_pip = round_state.pips[1-active]
        my_stk = round_state.stacks[active]
        op_stk = round_state.stacks[1-active]
        cost   = op_pip - my_pip            # chips to call
        pot    = my_pip + op_pip

        bounty  = round_state.bounties[active]
        b_live  = bool(bounty) and any(c[0] == bounty for c in hole + board)

        # Correct raise bounds from engine
        min_r = max_r = None
        if RaiseAction in legal:
            min_r, max_r = round_state.raise_bounds()

        # Clock guard — extremely fast fallback
        clk = game_state.game_clock
        if clk < 3.0:
            if cost == 0:
                return CheckAction() if CheckAction in legal else FoldAction()
            if cost < pot:   return CallAction()
            return FoldAction() if FoldAction in legal else CallAction()

        # ── PREFLOP ───────────────────────────────────────────────────────────
        if street == 0:
            return self._preflop(legal, hole, my_pip, op_pip, my_stk,
                                 cost, pot, active, b_live, min_r, max_r)

        # ── POSTFLOP ──────────────────────────────────────────────────────────
        # Sim budget: fewer sims on low clock
        n = 50 if clk > 30 else (30 if clk > 10 else 15)
        mine = to_ints(hole)
        brd  = to_ints(board)
        eq   = mc_equity(mine, brd, n)
        if b_live: eq = min(eq + 0.10, 0.99)

        return self._postflop(legal, eq, my_pip, op_pip, my_stk, op_stk,
                              cost, pot, street, b_live, min_r, max_r)

    # ── Preflop ───────────────────────────────────────────────────────────────
    def _preflop(self, legal, hole, my_pip, op_pip, my_stk,
                 cost, pot, active, b_live, min_r, max_r):
        r0 = rank(hole[0]); r1 = rank(hole[1])
        hi, lo  = max(r0, r1), min(r0, r1)
        suited  = hole[0][1] == hole[1][1]
        tier    = pf_tier(hi, lo, suited)
        if b_live and tier > 1: tier -= 1       # bounty = one tier better
        is_btn  = (active == 0)                 # BTN=SB in HU

        # ── Facing a raise ────────────────────────────────────────────────────
        if cost > 0:
            self.opp.pfr  += 1
            self.opp.vpip += 1
            ratio = cost / max(my_stk, 1)

            if tier == 1:                        # premium: 3-bet or call
                if RaiseAction in legal:
                    amt = min(max(op_pip * 3, min_r), max_r)
                    return RaiseAction(amt)
                return CallAction()

            if tier <= 3 and ratio < 0.50:       # decent hand, not too expensive
                return CallAction()

            if tier == 4 and ratio < 0.15:       # marginal, only cheap
                return CallAction()

            # Trash or too expensive → fold
            if FoldAction in legal and ratio > 0.20:
                return FoldAction()
            return CallAction()

        # ── No raise / open action ────────────────────────────────────────────
        if is_btn:
            # BTN (SB) opens aggressively in HU — very wide range
            if RaiseAction in legal:
                if tier <= 4 or b_live:
                    sz = min(max(int(pot * 2.5), min_r), max_r)
                    return RaiseAction(sz)
                # Even trash: raise 35% to stay balanced
                if random.random() < 0.35:
                    return RaiseAction(min_r)
            return CheckAction() if CheckAction in legal else CallAction()
        else:
            # BB gets a free look; squeeze strong, else check
            if tier <= 2 and RaiseAction in legal:
                sz = min(max(int(pot * 3), min_r), max_r)
                return RaiseAction(sz)
            return CheckAction() if CheckAction in legal else CallAction()

    # ── Postflop ──────────────────────────────────────────────────────────────
    def _postflop(self, legal, eq, my_pip, op_pip, my_stk, op_stk,
                  cost, pot, street, b_live, min_r, max_r):
        spr = min(my_stk, op_stk) / max(pot, 1)

        # Loose/passive opponent → value bet thinner; unknown → stay normal
        val_thresh   = 0.50 if (self.opp.is_loose() and self.opp.is_passive()) else 0.55
        raise_thresh = 0.68
        fe = self.opp.fold_eq()

        # ── Facing a bet ──────────────────────────────────────────────────────
        if cost > 0:
            self.opp.bets += 1
            if self._bet: self.opp.saw += 1

            pot_odds = cost / max(pot + cost, 1)

            # Value raise
            if eq >= raise_thresh and RaiseAction in legal:
                sz = self._raise_sz(pot, eq, spr, min_r, max_r, street)
                self._bet = True
                return RaiseAction(sz)

            # Call if equity justifies it (positive EV)
            ev = eq * (pot + cost) - (1-eq) * cost
            if eq > pot_odds + 0.02 or ev > 0:
                return CallAction()

            # Fold
            if FoldAction in legal:
                if self._bet: self.opp.fcb += 1
                return FoldAction()
            return CallAction()

        # ── First to act / check ──────────────────────────────────────────────
        if CheckAction not in legal and RaiseAction not in legal:
            return CallAction()

        # Value bet
        if eq >= val_thresh and RaiseAction in legal:
            sz = self._bet_sz(pot, eq, spr, min_r, max_r, street)
            self._bet = True
            return RaiseAction(sz)

        # Bluff on flop/turn (not river) with fold equity
        if (0.33 < eq < val_thresh and street < 5
                and RaiseAction in legal and fe > 0.48
                and not self.opp.is_loose()):
            sz = min(max(int(pot * 0.5), min_r), max_r)
            self._bet = True
            return RaiseAction(sz)

        return CheckAction() if CheckAction in legal else CallAction()

    # ── Sizing helpers ────────────────────────────────────────────────────────
    def _bet_sz(self, pot, eq, spr, min_r, max_r, street):
        if spr <= 1.5: return max_r
        f = (1.1 if street == 5 and eq > 0.85
             else 0.80 if eq > 0.75
             else 0.60 if eq > 0.62
             else 0.45)
        return max(min_r, min(int(pot * f), max_r))

    def _raise_sz(self, pot, eq, spr, min_r, max_r, street):
        if spr <= 1.5: return max_r
        f = 2.5 if eq > 0.85 else 1.8
        return max(min_r, min(int(pot * f), max_r))


if __name__ == '__main__':
    run_bot(Player(), parse_args())
 
