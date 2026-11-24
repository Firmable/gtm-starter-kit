#!/usr/bin/env node
/** Command line entry point: gtm-crm-enrichment. */
import { runMain } from "firmable-gtm-client";
import { RECIPE } from "./recipe.js";

runMain(RECIPE);
