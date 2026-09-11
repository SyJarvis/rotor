export const TIME_RANGES = [
  { key: "today", label: "今天", bucket: "hour" },
  { key: "24h", label: "24小时", bucket: "hour", hours: 24 },
  { key: "7d", label: "7天", bucket: "day", hours: 24 * 7 },
  { key: "30d", label: "30天", bucket: "day", hours: 24 * 30 },
];

export function createTimeRange(key, displayTimezone, now = new Date()) {
  const definition = TIME_RANGES.find((item) => item.key === key) || TIME_RANGES[2];
  if (definition.key === "today") {
    const periodDate = dateInTimezone(now, displayTimezone);
    return {
      ...definition,
      periodDate,
      startDate: periodDate,
      endDate: periodDate,
      query: new URLSearchParams({
        period: "day",
        period_date: periodDate,
      }).toString(),
    };
  }

  const end = new Date(now.getTime());
  const start = new Date(end.getTime() - definition.hours * 3600_000);
  return {
    ...definition,
    start,
    end,
    startDate: dateInTimezone(start, displayTimezone),
    endDate: dateInTimezone(end, displayTimezone),
    query: new URLSearchParams({
      start_time: start.toISOString(),
      end_time: end.toISOString(),
    }).toString(),
  };
}

export function createCustomTimeRange(startDate, endDate) {
  return {
    key: "custom",
    bucket: startDate === endDate ? "hour" : "day",
    apiBucket: "hour",
    startDate,
    endDate,
    query: new URLSearchParams({
      start_date: startDate,
      end_date: endDate,
    }).toString(),
  };
}

export function dateInTimezone(value, displayTimezone) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: displayTimezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(value)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

export function expectedRangeBuckets(range, displayTimezone) {
  if (range.key === "today" || (range.key === "custom" && range.bucket === "hour")) {
    return Array.from(
      { length: 24 },
      (_, hour) => `${range.startDate}T${String(hour).padStart(2, "0")}:00:00`,
    );
  }
  if (range.key === "custom") {
    const start = new Date(`${range.startDate}T00:00:00Z`);
    const end = new Date(`${range.endDate}T00:00:00Z`);
    const buckets = [];
    for (const cursor = start; cursor <= end; cursor.setUTCDate(cursor.getUTCDate() + 1)) {
      buckets.push(cursor.toISOString().slice(0, 10));
    }
    return buckets;
  }

  const step = range.bucket === "hour" ? 3600_000 : 86400_000;
  const cursor = new Date(range.start.getTime());
  if (range.bucket === "hour") {
    cursor.setUTCMinutes(0, 0, 0);
  } else {
    cursor.setUTCHours(0, 0, 0, 0);
  }
  const buckets = [];
  const seen = new Set();
  while (cursor < range.end) {
    const key = bucketKey(cursor.toISOString(), range.bucket, displayTimezone);
    if (!seen.has(key)) {
      seen.add(key);
      buckets.push(key);
    }
    cursor.setTime(cursor.getTime() + step);
  }
  return buckets;
}

export function bucketKey(bucket, resolution, displayTimezone) {
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
  return resolution === "hour" ? `${day}T${parts.hour}:00:00` : day;
}

export function bucketLabel(bucket, range) {
  if (range.bucket !== "hour") return bucket.slice(5);
  if (range.key === "today" || range.key === "custom") return bucket.slice(11, 16);
  return `${bucket.slice(5, 10)} ${bucket.slice(11, 16)}`;
}
