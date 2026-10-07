# Movie Systems Design 

## Pre-requisites

* An IDE or text editor of your choice
* [Sqlite3](http://www.sqlitetutorial.net/)
* Environment: Choose either Python 3.x or TypeScript with Node.js (v18+). For Python, install necessary libraries as needed (e.g., sqlite3 (built-in), pandas, numpy, openai). For TypeScript, run npm init, install deps like sqlite3, @types/node, @openai/openai, and use ts-node or compile to JS for execution. Justify any additional choices (e.g., for embeddings or prompting chains) in your README.


## Overview
This assessment is designed for a Senior AI Engineer role and should take approximately 2-3 hours to complete. It focuses on testing your problem-solving skills, coding proficiency (in either Python or TypeScript with Node.js), data handling, SQL querying, and expertise in integrating and leveraging large language models (LLMs) for AI applications, with a strong emphasis on prompt engineering, LLM output generation, and evaluation. You'll work with a provided SQLite database containing movie-related data. The emphasis is on using LLMs to enrich data, generate outputs, and build intelligent systems through effective prompting.

**The Databases**
- The databases are provided as a SQLite3 database in `db/`.  It does not require any credentials to login.  You can run SQL queries directly against the database using:

The movies table has the following columns:

* movieid: Unique identifier for each movie.
* title: Movie title.
* imdbid: IMDb identifier.
* overview: Brief description of the movie.
* productioncompanies: Pipe-separated list of production companies.
* releasedate: Release date of the movie.
* budget: Production budget in USD.
* revenue: Box office revenue in USD.
* runtime: Movie runtime in minutes.
* language: Primary language of the movie.
* genres: Pipe-separated list of genres (e.g., "Action|Drama").
* status: Release status (e.g., "Released").

## Tasks

#### Data Preparation & data enrichment
For a sample of 50-100 movies, Use prompts to generate additional 5 additional attributes for the provided movies data. 
Examples include:
* Sentiment analysis of movie overview (positive/negative/neutral).
* Categorize budget and revenue into tiers (e.g., low/medium/high) via LLM reasoning.
* Production Effectiveness Score using rating, budget, revenue

#### Movie System Design
Develop an LLM-integrated system for movie-related tasks (e.g., recommendations, rating predictions, or natural language querying)

* Design a system that leverages the prepared movie data and LLMs to generate outputs like personalized movie recommendations, user preference summaries, & comparative analyses (e.g., comparing movies based on budget, revenue, or runtime).
* Demonstrate prompting techniques to generate specific structured outputs (e.g., provide 5-10 example ratings or movie details for prediction tasks). Test with varied inputs (e.g., "Recommend action movies with high revenue and positive sentiment" or "Summarize preferences for user based on their ratings and movie overviews").


## Submission
We prefer you upload your project to GitHub and send us a link to your repo but you can also zip up the source and send it back to us. 

