package app.ampi.companion;

import android.app.Activity;
import android.content.Context;
import android.content.res.ColorStateList;
import android.content.res.Configuration;
import android.graphics.Insets;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/** A few views in the phone's own colours (no layout files, no libraries). */
final class Ui {
    static final int FULL = ViewGroup.LayoutParams.MATCH_PARENT, WRAP = ViewGroup.LayoutParams.WRAP_CONTENT;

    private Ui() {}

    static boolean dark(Context c) {
        return (c.getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
    }

    static int background(Context c) {
        return c.getColor(dark(c) ? android.R.color.system_neutral1_900 : android.R.color.system_neutral1_50);
    }

    static int card(Context c) {
        return c.getColor(dark(c) ? android.R.color.system_neutral1_800 : android.R.color.system_neutral1_10);
    }

    static int text(Context c) {
        return c.getColor(dark(c) ? android.R.color.system_neutral1_50 : android.R.color.system_neutral1_900);
    }

    static int quiet(Context c) {
        return c.getColor(dark(c) ? android.R.color.system_neutral2_300 : android.R.color.system_neutral2_700);
    }

    static int accent(Context c) {
        return c.getColor(dark(c) ? android.R.color.system_accent1_200 : android.R.color.system_accent1_600);
    }

    static int onAccent(Context c) {
        return c.getColor(dark(c) ? android.R.color.system_accent1_800 : android.R.color.system_accent1_0);
    }

    static int dp(Context c, float n) {
        return Math.round(n * c.getResources().getDisplayMetrics().density);
    }

    /** A scrolling column that keeps clear of the system bars. */
    static LinearLayout page(Activity a) {
        LinearLayout col = new LinearLayout(a);
        col.setOrientation(LinearLayout.VERTICAL);
        ScrollView scroll = new ScrollView(a);
        scroll.setFillViewport(true);
        scroll.setClipToPadding(false);
        scroll.addView(col, FULL, WRAP);
        scroll.setBackgroundColor(background(a));
        int side = dp(a, 16);
        scroll.setOnApplyWindowInsetsListener((v, insets) -> {
            Insets i = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            v.setPadding(side + i.left, i.top + dp(a, 12), side + i.right, i.bottom + dp(a, 24));
            return WindowInsets.CONSUMED;
        });
        a.setContentView(scroll);
        int bars = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
        a.getWindow().getInsetsController().setSystemBarsAppearance(dark(a) ? 0 : bars, bars);
        return col;
    }

    static LinearLayout card(LinearLayout parent) {
        Context c = parent.getContext();
        LinearLayout box = new LinearLayout(c);
        box.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(card(c));
        bg.setCornerRadius(dp(c, 20));
        box.setBackground(bg);
        box.setPadding(dp(c, 18), dp(c, 16), dp(c, 18), dp(c, 16));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(FULL, WRAP);
        lp.topMargin = dp(c, 12);
        parent.addView(box, lp);
        return box;
    }

    static TextView add(LinearLayout parent, CharSequence s, float sp, int color) {
        TextView t = new TextView(parent.getContext());
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        t.setAutoLinkMask(0);
        t.setLineSpacing(0, 1.15f);
        parent.addView(t, FULL, WRAP);
        return t;
    }

    static TextView title(LinearLayout parent, CharSequence s) {
        TextView t = add(parent, s, 28, text(parent.getContext()));
        t.setTypeface(Typeface.create("sans-serif", Typeface.BOLD));
        t.setAccessibilityHeading(true);
        return t;
    }

    static TextView heading(LinearLayout parent, CharSequence s) {
        Context c = parent.getContext();
        TextView t = add(parent, s, 14, accent(c));
        t.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        t.setPadding(dp(c, 4), dp(c, 24), 0, 0);
        t.setAccessibilityHeading(true);
        return t;
    }

    static TextView body(LinearLayout parent, CharSequence s) {
        return add(parent, s, 16, text(parent.getContext()));
    }

    static TextView small(LinearLayout parent, CharSequence s) {
        return add(parent, s, 14, quiet(parent.getContext()));
    }

    static Button button(LinearLayout parent, String label, boolean strong, View.OnClickListener click) {
        Context c = parent.getContext();
        Button b = new Button(c);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(15);
        b.setTextColor(strong ? onAccent(c) : accent(c));
        GradientDrawable bg = new GradientDrawable();
        bg.setCornerRadius(dp(c, 24));
        if (strong) bg.setColor(accent(c));
        else bg.setStroke(dp(c, 1), accent(c));
        b.setBackground(new RippleDrawable(ColorStateList.valueOf((accent(c) & 0xffffff) | 0x33000000), bg, null));
        b.setStateListAnimator(null);
        b.setMinHeight(dp(c, 48));
        b.setGravity(Gravity.CENTER);
        b.setPadding(dp(c, 20), 0, dp(c, 20), 0);
        b.setOnClickListener(click);
        boolean row = parent.getOrientation() == LinearLayout.HORIZONTAL;
        LinearLayout.LayoutParams lp = row ? new LinearLayout.LayoutParams(0, WRAP, 1) : new LinearLayout.LayoutParams(FULL, WRAP);
        lp.topMargin = dp(c, 12);
        if (row && parent.getChildCount() > 0) lp.leftMargin = dp(c, 12);
        parent.addView(b, lp);
        return b;
    }

    static LinearLayout row(LinearLayout parent) {
        LinearLayout r = new LinearLayout(parent.getContext());
        r.setOrientation(LinearLayout.HORIZONTAL);
        r.setGravity(Gravity.CENTER_VERTICAL);
        parent.addView(r, FULL, WRAP);
        return r;
    }
}
