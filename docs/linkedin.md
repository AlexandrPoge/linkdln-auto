# LinkedIn workflow

LinkedIn's [Job Alerts](https://www.linkedin.com/help/linkedin/answer/a511279/job-alerts-on-linkedin) can run the search inside LinkedIn and notify the member of new matches. Configure two separate remote searches in the LinkedIn Jobs UI:

1. `Automation Engineer` with the remote filter and Belarus as location.
2. `Automation Engineer` with the remote filter and a broader European location supported by the UI. Treat this as discovery, not proof that the employer hires remote workers resident in Belarus.

Turn on a Job Alert for each search. Check the location and hiring-eligibility text in every listing. Use the local app's Belarus and international queue tabs for vacancies imported from its supported public sources. Each matched card contains a short LinkedIn recruiter-message draft that can be copied after reviewing the role and identifying the actual recipient. The draft does **not** imply that a LinkedIn alert has been imported into the app or that a message has been sent. The app currently has no LinkedIn listing importer.

LinkedIn [does not permit third-party software that automates its website](https://www.linkedin.com/help/linkedin/answer/a1340567/automated-activity-on-linkedin), including unauthorized bots that [send messages or scrape data](https://www.linkedin.com/help/linkedin/answer/a1341387/prohibition-of-scraping-software). Its [general developer permissions](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access) do not grant unrestricted access to member data; most permissions require approval. For this personal project we do not use browser automation, private endpoints, cookies, or a credential-storing extension to search or send messages through LinkedIn.

To integrate job alerts into the local queue later, supply a redacted example of a LinkedIn Job Alert email or another officially supported export. Do not share a password, session cookie, or one-time login code. We can then validate a read-only importer against the actual format before connecting it to the review queue.
