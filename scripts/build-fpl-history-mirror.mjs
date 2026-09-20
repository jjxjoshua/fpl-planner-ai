import fs from "node:fs/promises";
import path from "node:path";

const API_BASE = "https://fantasy.premierleague.com/api";
const USER_AGENT = "jjxjoshua-fpl-history-mirror/1.0 (read-only daily backup)";
const REQUEST_TIMEOUT_MS = 45_000;
const MAX_ATTEMPTS = 3;
const PLAYER_DELAY_MS = 120;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function num(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function intOrNull(value) {
  if (value == null || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

function numOrNull(value) {
  if (value == null || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function bool(value) {
  return value === true || value === 1 || value === "true";
}

function positionOf(elementType) {
  return ["", "GKP", "DEF", "MID", "FWD", "AM"][num(elementType)] ?? "";
}

async function fetchJson(url) {
  let lastError;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      const response = await fetch(url, {
        headers: { Accept: "application/json", "User-Agent": USER_AGENT },
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status} for ${url}`);
      }
      return await response.json();
    } catch (error) {
      lastError = error;
      if (attempt < MAX_ATTEMPTS) await sleep(attempt * 1_500);
    }
  }
  throw lastError;
}

function currentSeasonStartYear(now = new Date()) {
  return now.getUTCMonth() >= 6 ? now.getUTCFullYear() : now.getUTCFullYear() - 1;
}

function seasonLabel(year) {
  return `${year}-${String((year + 1) % 100).padStart(2, "0")}`;
}

function resolveCurrentGameweek(events) {
  const marked = events.find((event) => bool(event.is_current)) ?? events.find((event) => bool(event.is_next));
  if (marked?.id) return num(marked.id);
  return events
    .filter((event) => bool(event.finished))
    .map((event) => num(event.id))
    .reduce((max, value) => Math.max(max, value), 0);
}

async function main() {
  const outputPath = path.resolve(process.argv[2] ?? "history/current-history.json");
  const bootstrap = await fetchJson(`${API_BASE}/bootstrap-static/`);
  const events = Array.isArray(bootstrap.events) ? bootstrap.events : [];
  const teams = Array.isArray(bootstrap.teams) ? bootstrap.teams : [];
  const elements = Array.isArray(bootstrap.elements) ? bootstrap.elements : [];

  if (events.length < 1) throw new Error("bootstrap.events missing or empty");
  if (teams.length < 20) throw new Error("bootstrap.teams is incomplete");
  if (elements.length < 300) throw new Error("bootstrap.elements is incomplete");

  const startYear = currentSeasonStartYear();
  const label = seasonLabel(startYear);
  const totalPlayers = intOrNull(bootstrap.total_players) ?? 0;
  const currentGameweek = resolveCurrentGameweek(events);
  const dataCheckedByGw = new Map(events.map((event) => [num(event.id), bool(event.data_checked)]));
  const rows = [];
  const seen = new Set();

  for (let index = 0; index < elements.length; index++) {
    const player = elements[index];
    const elementId = num(player.id);
    const playerCode = num(player.code);
    if (elementId <= 0 || playerCode <= 0) {
      throw new Error(`invalid player identity at index ${index}`);
    }

    const summary = await fetchJson(`${API_BASE}/element-summary/${elementId}/`);
    const history = Array.isArray(summary.history) ? summary.history : [];
    for (const item of history) {
      const gameweek = num(item.round);
      const fixtureId = intOrNull(item.fixture);
      if (gameweek <= 0 || fixtureId == null || fixtureId <= 0) {
        throw new Error(`invalid history identity for element ${elementId}`);
      }
      const uniqueKey = `${playerCode}:${fixtureId}`;
      if (seen.has(uniqueKey)) throw new Error(`duplicate history key ${uniqueKey}`);
      seen.add(uniqueKey);

      const selected = numOrNull(item.selected);
      const ownershipPercent = selected != null && totalPlayers > 0
        ? Math.round((selected / totalPlayers) * 10_000) / 100
        : null;

      rows.push({
        playerCode,
        elementId,
        fixtureId,
        name: player.web_name ?? "",
        position: positionOf(player.element_type),
        teamId: intOrNull(player.team),
        gameweek,
        minutes: num(item.minutes),
        starts: intOrNull(item.starts),
        goals: num(item.goals_scored),
        assists: num(item.assists),
        cleanSheets: num(item.clean_sheets),
        goalsConceded: num(item.goals_conceded),
        ownGoals: num(item.own_goals),
        penaltiesSaved: num(item.penalties_saved),
        penaltiesMissed: num(item.penalties_missed),
        yellowCards: num(item.yellow_cards),
        redCards: num(item.red_cards),
        saves: num(item.saves),
        bonus: num(item.bonus),
        bps: num(item.bps),
        totalPoints: num(item.total_points),
        expectedGoals: numOrNull(item.expected_goals),
        expectedAssists: numOrNull(item.expected_assists),
        expectedGoalsConceded: numOrNull(item.expected_goals_conceded),
        expectedGoalInvolvements: numOrNull(item.expected_goal_involvements),
        ictIndex: numOrNull(item.ict_index),
        value: intOrNull(item.value),
        selectedCount: selected == null ? null : Math.round(selected),
        ownershipPercent,
        totalEntries: totalPlayers || null,
        rawSelected: selected,
        dataChecked: dataCheckedByGw.get(gameweek) === true,
        wasHome: item.was_home === true,
        opponentTeamId: intOrNull(item.opponent_team),
      });
    }

    if ((index + 1) % 50 === 0 || index + 1 === elements.length) {
      console.log(`Fetched ${index + 1}/${elements.length} player summaries; rows=${rows.length}`);
    }
    await sleep(PLAYER_DELAY_MS);
  }

  if (rows.length < 300) throw new Error(`history snapshot unexpectedly small: ${rows.length} rows`);

  const snapshot = {
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    source: "official-fpl-api",
    seasonLabel: label,
    startYear,
    currentGameweek,
    totalPlayers,
    playerCount: elements.length,
    rowCount: rows.length,
    players: elements.map((player) => ({
      elementId: num(player.id),
      playerCode: num(player.code),
      webName: player.web_name ?? null,
      teamId: intOrNull(player.team),
      position: positionOf(player.element_type),
    })),
    teams: teams.map((team) => ({
      teamId: num(team.id),
      name: team.name ?? String(team.id),
      shortName: team.short_name ?? null,
      teamCode: intOrNull(team.code),
    })),
    gameweeks: events.map((event) => ({
      gameweek: num(event.id),
      name: event.name ?? null,
      deadline: event.deadline_time ?? null,
      isCurrent: bool(event.is_current),
      isNext: bool(event.is_next),
      finished: bool(event.finished),
      dataChecked: bool(event.data_checked),
      totalEntries: totalPlayers || null,
      highestScore: intOrNull(event.highest_score),
    })),
    rows,
  };

  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.writeFile(outputPath, JSON.stringify(snapshot) + "\n", "utf8");
  console.log(`Wrote ${rows.length} rows for ${label} to ${outputPath}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
