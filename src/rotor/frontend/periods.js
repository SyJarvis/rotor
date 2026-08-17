export const CALENDAR_PERIODS = [
  { key: "day", label: "当天", bucket: "hour" },
  { key: "week", label: "本周", bucket: "day" },
  { key: "month", label: "本月", bucket: "day" },
];

export function localDate() {
  const now = new Date();
  return isoDate(now.getFullYear(), now.getMonth() + 1, now.getDate());
}

export function todayInTimezone(displayTimezone) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: displayTimezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date())
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

export function periodQuery(period, periodDate) {
  return new URLSearchParams({
    period,
    period_date: periodDate,
  }).toString();
}

export function periodDates(period, periodDate) {
  const [year, month, day] = periodDate.split("-").map(Number);
  const anchor = new Date(Date.UTC(year, month - 1, day));
  if (period === "day") return [periodDate];
  if (period === "week") {
    const mondayOffset = (anchor.getUTCDay() + 6) % 7;
    const start = addDays(anchor, -mondayOffset);
    return Array.from({ length: 7 }, (_, index) => isoDateFromDate(addDays(start, index)));
  }
  const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return Array.from({ length: days }, (_, index) => isoDate(year, month, index + 1));
}

export function bucketKey(bucket, period, displayTimezone) {
  const value = String(bucket || "");
  const date = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return value;
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: displayTimezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      hourCycle: "h23",
    }).formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  const day = `${parts.year}-${parts.month}-${parts.day}`;
  return period === "day" ? `${day}T${parts.hour}:00:00` : day;
}

function addDays(value, days) {
  return new Date(value.getTime() + days * 86400_000);
}

function isoDateFromDate(value) {
  return isoDate(value.getUTCFullYear(), value.getUTCMonth() + 1, value.getUTCDate());
}

function isoDate(year, month, day) {
  return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}
