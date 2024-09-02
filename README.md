mc-registration
===============

The mc-registration app provides the user-facing convention registration
and administrative reg capability on the Motor City Furry Con website.

Live demo: https://motorcityfurrycon.org/register/ (Well, usually best
when the convention is gearing up.)

**NB: This is an early fork from MCFC's monolithic website repo.** It's
very much a work in progress from that perspective. Keep that in mind if
you're trying to use this; you're welcome to grab a copy and tinker with
it until it works, but this repo is likely to see some substantial
changes until that dust settles.

That primarily centers around the payment processing, which had been
embedded in the old version, and will likely be carved off to a separate
repo which you'll (hopefully) be able to better adjust for your own
providers/situations.

# Features

* TBD

# Installation

It requires a Convention parent object, representing the convention year
that the schedule is for. This by default expects to come from the
[mc-convention](https://github.com/drykath/mc-convention) app, but that
can be overridden if you already have a model that represents that. See
the [mc-convention](https://github.com/drykath/mc-convention) docs for
how to set `CONVENTION_MODEL` to something else.

This still uses some other interfaces from that app, so:

    INSTALLED_APPS = [
        ....
        'convention',
        'registration',
        ....
    ]

And customize the templates/CSS styles as needed. If you use the provided templates make sure the `APP_DIRS` key is enabled in the `TEMPLATES` settings, or just copy or make your own as needed.

# Known Issues

* TBD, but primarily see note above.
