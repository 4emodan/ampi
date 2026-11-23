package app.ampi.companion;

import java.time.DayOfWeek;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.Set;

/** When an alarm rings next. Wall-clock times follow the phone's current time zone. */
final class AlarmTimes {
    private AlarmTimes() {}

    /**
     * The next moment (epoch ms) strictly after {@code now}, or -1 if there is none (a date in the past).
     * date: that day only. days: every such weekday. Neither: the next time the clock shows {@code time}.
     */
    static long next(LocalTime time, LocalDate date, Set<DayOfWeek> days, ZoneId zone, long now) {
        ZonedDateTime from = Instant.ofEpochMilli(now).atZone(zone);
        if (date != null) {
            long at = at(date, time, zone);
            return at > now ? at : -1;
        }
        boolean daily = days == null || days.isEmpty();
        for (int i = 0; i <= 8; i++) {
            LocalDate d = from.toLocalDate().plusDays(i);
            if (!daily && !days.contains(d.getDayOfWeek())) continue;
            long at = at(d, time, zone);
            if (at > now) return at;
        }
        return -1;
    }

    /**
     * After the clock or the zone changed: the moment of the occurrence that was set for {@code day}. Past: that one was
     * jumped over (the caller rings or reports it). Still to come: the next one from now, which may be an earlier day if
     * the clock went back.
     */
    static long again(LocalTime time, LocalDate date, Set<DayOfWeek> days, LocalDate day, ZoneId zone, long now) {
        long same = at(day, time, zone);
        return same <= now ? same : next(time, date, days, zone, now);
    }

    /**
     * A time skipped by the spring clock change rings right after the jump (03:30 becomes 04:30); a time that happens
     * twice in autumn rings the first time only.
     */
    static long at(LocalDate d, LocalTime t, ZoneId zone) {
        // ofLocal: a gap moves the time forward by its length, an overlap takes the earlier offset.
        return ZonedDateTime.ofLocal(d.atTime(t), zone, null).toInstant().toEpochMilli();
    }
}
