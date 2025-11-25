#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PokerOK / GGNetwork Hand History Analyzer
"""

import argparse
import csv
import logging
import os
import re
from pathlib import Path
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Iterable
from statistics import mean


# =============================================================
#                     DATA CLASSES 
# =============================================================

@dataclass
class Player:
    seat: int
    name: str
    stack: float


@dataclass
class Action:
    street: str            # 'preflop' | 'flop' | 'turn' | 'river'
    actor: str
    action: str            # fold/call/check/bet/raise/all-in
    amount: Optional[float]


@dataclass
class Hand:
    hand_id: str
    datetime: Optional[str]
    table: str
    sb: float
    bb: float
    hero: str
    hero_cards: List[str] = field(default_factory=list)
    players: List[Player] = field(default_factory=list)
    actions: List[Action] = field(default_factory=list)
    board: List[str] = field(default_factory=list)
    hero_result: float = 0.0


# =============================================================
#                     PARSER FUNCTIONS
# =============================================================

HAND_SPLIT_RE = re.compile(r"^Poker Hand #", re.MULTILINE)

# VERY LIGHT parser for PokerOK / GG HH — this is an MVP
# You will extend it for your format variant.

def iter_hand_texts(text: str) -> Iterable[str]:
    """Разбивает файл на блоки-руки."""
    parts = HAND_SPLIT_RE.split(text)
    if not parts or len(parts) <= 1:
        return
    for part in parts[1:]:
        yield "Poker Hand #" + part


def parse_hand(hand_text: str, hero_name: str) -> Optional[Hand]:
    """Парсинг одной раздачи. MVP-парсер."""
    try:
        # ---------------- HEADER ----------------
        id_match = re.search(r"Poker Hand #(\d+)", hand_text)
        table_match = re.search(r"Table\s+(.+)", hand_text)
        stake_match = re.search(r"Blinds\s+(\S+)/(\S+)", hand_text)

        hand_id = id_match.group(1) if id_match else "UNKNOWN"
        table = table_match.group(1).strip() if table_match else "UnknownTable"

        if stake_match:
            sb = float(stake_match.group(1).replace("$", ""))
            bb = float(stake_match.group(2).replace("$", ""))
        else:
            sb = 0.5
            bb = 1.0

        # ---------------- PLAYERS ----------------
        players = []
        for seat, name, stack in re.findall(r"Seat (\d+): (\S+) \(([\d\.]+)\)", hand_text):
            players.append(Player(int(seat), name, float(stack)))

        # ---------------- HERO CARDS ----------------
        hero_cards = []
        hero_cards_match = re.search(
            hero_name + r": Card dealt to \S+ \[(\S+) (\S+)\]", hand_text
        )
        if hero_cards_match:
            hero_cards = [hero_cards_match.group(1), hero_cards_match.group(2)]

        # ---------------- BOARD ----------------
        board = []
        board_match = re.search(r"Board \[(.*?)\]", hand_text)
        if board_match:
            board = board_match.group(1).split()

        # ---------------- ACTIONS ----------------
        actions = []
        street = None
        for line in hand_text.splitlines():
            line = line.strip()

            if line.startswith("***"):
                if "FLOP" in line:
                    street = "flop"
                elif "TURN" in line:
                    street = "turn"
                elif "RIVER" in line:
                    street = "river"
                else:
                    street = "preflop"
                continue

            m = re.match(r"(\S+): (folds|checks|calls|bets|raises|all-in)(?: (\$?[\d\.]+))?", line)
            if m and street:
                actor = m.group(1)
                action = m.group(2)
                amount = m.group(3)
                if amount:
                    amount = float(amount.replace("$", ""))
                actions.append(Action(street, actor, action, amount))

        # ---------------- HERO RESULT ----------------
        result = 0.0
        win = re.search(hero_name + r" collected \$?([\d\.]+)", hand_text)
        if win:
            result += float(win.group(1))

        lose = re.search(hero_name + r" lost \$?([\d\.]+)", hand_text)
        if lose:
            result -= float(lose.group(1))

        return Hand(
            hand_id=hand_id,
            datetime=None,
            table=table,
            sb=sb,
            bb=bb,
            hero=hero_name,
            hero_cards=hero_cards,
            players=players,
            actions=actions,
            board=board,
            hero_result=result
        )
    except Exception as e:
        logging.debug(f"Parse error in hand: {e}")
        return None


# =============================================================
#                     POSITION LOGIC
# =============================================================

def determine_positions(hand: Hand) -> Dict[str, str]:
    """Возвращает словарь {player_name -> position}."""
    # MVP: просто определить BTN как seat с наименьшим номером
    if not hand.players:
        return {}
    sorted_players = sorted(hand.players, key=lambda p: p.seat)
    positions = ["BTN", "SB", "BB", "UTG", "MP", "CO"]
    pos_map = {}
    for i, p in enumerate(sorted_players):
        pos_map[p.name] = positions[i % len(positions)]
    return pos_map


# =============================================================
#                     STATS AGGREGATOR
# =============================================================

@dataclass
class StatsAggregator:
    hero: str
    hands: List[Hand] = field(default_factory=list)

    # aggregated fields
    total_won_chips: float = 0.0

    vpip: int = 0
    pfr: int = 0
    hands_count: int = 0

    threebet: int = 0
    threebet_opportunities: int = 0

    fold_to_3bet: int = 0
    vs_3bet_opportunities: int = 0

    cbet_flop: int = 0
    cbet_flop_opportunities: int = 0

    cbet_turn: int = 0
    cbet_turn_opportunities: int = 0

    wtsd: int = 0
    wsd_wins: int = 0
    saw_flop: int = 0

    pos_stats: Dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))

    def add_hand(self, hand: Hand):
        self.hands.append(hand)
        self.hands_count += 1
        self.total_won_chips += hand.hero_result

        positions = determine_positions(hand)
        hero_pos = positions.get(self.hero, "UNKNOWN")
        self.pos_stats[hero_pos]["hands"] += 1

        # VPIP / PFR
        preflop_actions = [a for a in hand.actions if a.street == "preflop" and a.actor == self.hero]
        did_vpip = any(a.action in ("calls", "raises", "bets", "all-in") for a in preflop_actions)
        did_pfr = any(a.action in ("raises", "bets", "all-in") for a in preflop_actions)

        if did_vpip:
            self.vpip += 1
            self.pos_stats[hero_pos]["vpip"] += 1
        if did_pfr:
            self.pfr += 1
            self.pos_stats[hero_pos]["pfr"] += 1

        # Flop C-bet
        flop_actions = [a for a in hand.actions if a.street == "flop"]
        if flop_actions:
            self.saw_flop += 1

        # WTSD / W$SD (MVP)
        if hand.board and len(hand.board) == 5:
            self.wtsd += 1
            if hand.hero_result > 0:
                self.wsd_wins += 1

        # Flop C-bet opportunity (if hero was PFR)
        if did_pfr and flop_actions:
            self.cbet_flop_opportunities += 1
            first_flop_actor = flop_actions[0].actor
            if first_flop_actor == self.hero:
                self.cbet_flop += 1
                self.pos_stats[hero_pos]["cbet_flop"] += 1

        # Turn C-bet opportunity
        turn_actions = [a for a in hand.actions if a.street == "turn"]
        if did_pfr and turn_actions:
            self.cbet_turn_opportunities += 1
            first_turn_actor = turn_actions[0].actor
            if first_turn_actor == self.hero:
                self.cbet_turn += 1
                self.pos_stats[hero_pos]["cbet_turn"] += 1

    # ---------------- STATS COMPUTATION ----------------

    def compute(self):
        stats = {}

        total = self.hands_count or 1

        stats["hands"] = self.hands_count
        stats["vpip_pct"] = round(self.vpip / total * 100, 2)
        stats["pfr_pct"] = round(self.pfr / total * 100, 2)
        stats["vpip_pfr_gap"] = round(stats["vpip_pct"] - stats["pfr_pct"], 2)

        stats["cbet_flop_pct"] = round(
            (self.cbet_flop / self.cbet_flop_opportunities * 100) if self.cbet_flop_opportunities else 0.0, 2
        )
        stats["cbet_turn_pct"] = round(
            (self.cbet_turn / self.cbet_turn_opportunities * 100) if self.cbet_turn_opportunities else 0.0, 2
        )

        stats["wtsd_pct"] = round(self.wtsd / (self.saw_flop or 1) * 100, 2)
        stats["wsd_pct"] = round(self.wsd_wins / (self.wtsd or 1) * 100, 2)

        stats["total_won_chips"] = round(self.total_won_chips, 2)

        # bb/100
        # MVP approach: bb average over all hands
        avg_bb = mean([h.bb for h in self.hands]) if self.hands else 1
        stats["bb_per_100"] = round(self.total_won_chips / avg_bb * 100 / total, 2)

        return stats

    def compute_positional(self):
        pos_output = {}
        for pos, cnt in self.pos_stats.items():
            hands = cnt.get("hands", 0)
            if hands == 0:
                continue
            vp = cnt.get("vpip", 0) / hands * 100
            pf = cnt.get("pfr", 0) / hands * 100
            cbf = cnt.get("cbet_flop", 0) / (cnt.get("cbet_flop_opportunities", hands) or 1) * 100

            # bb/100 by position — simplified
            results = [h.hero_result for h in self.hands if determine_positions(h).get(self.hero) == pos]
            bb100 = 0.0
            if results:
                avg_bb = mean([h.bb for h in self.hands])
                bb100 = sum(results) / avg_bb * 100 / len(results)

            pos_output[pos] = {
                "hands": hands,
                "vpip_pct": round(vp, 2),
                "pfr_pct": round(pf, 2),
                "bb_per_100": round(bb100, 2),
            }
        return pos_output


# =============================================================
#                     CSV EXPORT
# =============================================================

def save_csv_global(path, stats):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        for k, v in stats.items():
            writer.writerow([k, v])


def save_csv_positions(path, pos_stats):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["position", "hands", "vpip_pct", "pfr_pct", "bb_per_100"])
        for pos, data in pos_stats.items():
            writer.writerow([pos, data["hands"], data["vpip_pct"], data["pfr_pct"], data["bb_per_100"]])


# =============================================================
#                     REPORT PRINTING
# =============================================================

def print_report(stats, pos_stats):
    print("\n=== PokerOK / GGNetwork Hand History Summary ===\n")

    print(f"Total hands: {stats['hands']}")
    print(f"VPIP:        {stats['vpip_pct']} %")
    print(f"PFR:         {stats['pfr_pct']} %")
    print(f"Gap:         {stats['vpip_pfr_gap']} %\n")

    print(f"C-bet Flop:  {stats['cbet_flop_pct']} %")
    print(f"C-bet Turn:  {stats['cbet_turn_pct']} %")
    print(f"WTSD:        {stats['wtsd_pct']} %")
    print(f"W$SD:        {stats['wsd_pct']} %\n")

    print(f"Total result: {stats['total_won_chips']} chips")
    print(f"Winrate:      {stats['bb_per_100']} bb/100\n")

    print("=== Positional Stats ===")
    print("Position   Hands   VPIP%   PFR%   bb/100")
    print("------------------------------------------")
    for pos, data in pos_stats.items():
        print(f"{pos:<10} {data['hands']:<7} {data['vpip_pct']:<7} {data['pfr_pct']:<7} {data['bb_per_100']:<7}")
    print()


# =============================================================
#                     FILE DISCOVERY
# =============================================================

def find_files(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    return [f for f in path.rglob("*") if f.is_file() and f.suffix in (".txt", ".log")]


# =============================================================
#                     MAIN
# =============================================================

def main():
    parser = argparse.ArgumentParser(description="PokerOK / GGNetwork hand history analyzer")
    parser.add_argument("--path", required=True, help="Path to HH file or directory")
    parser.add_argument("--hero", default="Hero", help="Hero name")
    parser.add_argument("--output-prefix", default=None, help="Prefix for CSV output")
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    path = Path(args.path)
    files = find_files(path)
    logging.info(f"Found {len(files)} files")

    aggregator = StatsAggregator(hero=args.hero)

    for filepath in files:
        try:
            with open(filepath, "r", encoding=args.encoding, errors="ignore") as f:
                text = f.read()
        except Exception as e:
            logging.warning(f"Error reading file {filepath}: {e}")
            continue

        for block in iter_hand_texts(text):
            hand = parse_hand(block, args.hero)
            if hand:
                aggregator.add_hand(hand)
            else:
                logging.debug("Hand parsing failed")

    stats = aggregator.compute()
    pos_stats = aggregator.compute_positional()

    print_report(stats, pos_stats)

    if args.output_prefix:
        save_csv_global(args.output_prefix + "_global_stats.csv", stats)
        save_csv_positions(args.output_prefix + "_positional_stats.csv", pos_stats)
        print(f"\nCSV saved with prefix: {args.output_prefix}")


if __name__ == "__main__":
    main()