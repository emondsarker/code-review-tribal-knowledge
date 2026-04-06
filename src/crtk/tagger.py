from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from crtk.config import CrtkConfig
from crtk.db import add_comment_tag, ensure_tag, get_comments_by_ids

logger = logging.getLogger(__name__)


@dataclass
class TagRule:
    tag_name: str
    description: str
    body_patterns: list[str]  # regex patterns to match in comment body
    path_patterns: list[str]  # regex patterns to match in file path
    hunk_patterns: list[str]  # regex patterns to match in diff hunk


# Seed tag rules — keyword-based heuristics
SEED_TAG_RULES: list[TagRule] = [
    # Code quality
    TagRule("naming", "Naming conventions and variable/function names",
            [r"\brename\b", r"\bnamed?\b", r"\bshould be called\b", r"\bnaming\b", r"\bcamelCase\b", r"\bsnake_case\b"],
            [], []),
    TagRule("code-style", "Code style, formatting, and consistency",
            [r"\bformat\b", r"\bstyle\b", r"\blint\b", r"\bindent\b", r"\bconsisten"],
            [], []),
    TagRule("readability", "Code readability and clarity",
            [r"\breadab", r"\bclear\b", r"\bconfusing\b", r"\bhard to follow\b", r"\bsimplif"],
            [], []),
    TagRule("dead-code", "Dead code, unused imports, unreachable code",
            [r"\bunused\b", r"\bdead code\b", r"\bremove this\b", r"\bnot used\b", r"\bunreachable\b"],
            [], []),
    TagRule("duplication", "Code duplication and DRY principle",
            [r"\bduplic", r"\bDRY\b", r"\brepeat", r"\balready exist", r"\breuse\b"],
            [], []),
    TagRule("complexity", "Code complexity, simplification opportunities",
            [r"\bcomplex\b", r"\bsimplif", r"\bnested\b", r"\btoo (?:many|much|long)\b", r"\brefactor"],
            [], []),

    # Architecture
    TagRule("architecture", "Architecture decisions and patterns",
            [r"\barchitect", r"\bpattern\b", r"\bdesign\b", r"\bstructur", r"\bmodular"],
            [], []),
    TagRule("separation-of-concerns", "Separation of concerns and responsibility",
            [r"\bseparation\b", r"\bconcern", r"\bresponsib", r"\bsingle.?purpose\b", r"\bSRP\b"],
            [], []),
    TagRule("dependency-injection", "Dependency injection and IoC",
            [r"\binject", r"\bprovider\b", r"\bDI\b", r"\bIoC\b"],
            [], []),
    TagRule("module-structure", "Module organization and file structure",
            [r"\bmodule\b", r"\bfolder\b", r"\bdirectory\b", r"\borganiz", r"\bfile structure\b"],
            [], []),

    # Data
    TagRule("database", "Database operations, queries, schemas",
            [r"\bdatab", r"\bDB\b", r"\bschema\b", r"\btable\b", r"\bcolumn\b", r"\bindex\b"],
            [r"\.repository\.", r"\.entity\.", r"database"], [r"CREATE TABLE", r"ALTER TABLE"]),
    TagRule("migrations", "Database migrations",
            [r"\bmigrat"],
            [r"migration", r"\.migration\."], [r"CREATE TABLE", r"ALTER TABLE", r"ADD COLUMN"]),
    TagRule("n-plus-one-queries", "N+1 query problems and batch loading",
            [r"N\+1", r"\bn\+1\b", r"\bfindOne.*loop\b", r"\bfor.*each.*query\b", r"\bbatch.?load\b"],
            [], [r"findOne", r"\.find\(.*for"]),
    TagRule("data-modeling", "Data models, entities, DTOs",
            [r"\bentity\b", r"\bmodel\b", r"\bDTO\b", r"\bschema\b", r"\brelation"],
            [r"\.entity\.", r"\.dto\.", r"\.model\."], []),
    TagRule("caching", "Caching strategies and implementation",
            [r"\bcach", r"\bredis\b", r"\bttl\b", r"\binvalidat"],
            [], []),

    # API
    TagRule("api-design", "API design, endpoints, contracts",
            [r"\bendpoint\b", r"\bAPI\b", r"\broute\b", r"\bcontract\b", r"\bREST\b"],
            [r"controller", r"\.controller\."], []),
    TagRule("request-validation", "Request validation and input checking",
            [r"\bvalidat", r"\binput\b", r"\bpayload\b", r"\bbody\b.*\bcheck", r"\bpipe\b"],
            [r"\.pipe\.", r"\.dto\."], [r"@IsNotEmpty", r"@IsString", r"class-validator"]),
    TagRule("response-format", "Response format and serialization",
            [r"\bresponse\b", r"\bserializ", r"\btransform", r"\bformat\b"],
            [r"interceptor", r"\.serializer\."], []),
    TagRule("pagination", "Pagination implementation",
            [r"\bpaginat", r"\boffset\b", r"\blimit\b", r"\bcursor\b", r"\bpage\b"],
            [], []),
    TagRule("error-responses", "Error response format and HTTP status codes",
            [r"\bstatus code\b", r"\b[45]\d\d\b", r"\berror response\b", r"\bHttpException\b"],
            [], []),

    # Error handling
    TagRule("error-handling", "Error handling, try/catch, exceptions",
            [r"\berror\b", r"\bexception\b", r"\btry.?catch\b", r"\bthrow\b", r"\bhandle\b.*\berror"],
            [], [r"catch\s*\(", r"throw new"]),
    TagRule("null-safety", "Null checks, optional chaining, undefined handling",
            [r"\bnull\b", r"\bundefined\b", r"\boptional\b", r"\b\?\.\b", r"\bnullable\b"],
            [], []),
    TagRule("edge-cases", "Edge cases and boundary conditions",
            [r"\bedge case\b", r"\bboundary\b", r"\bcorner case\b", r"\bwhat if\b", r"\bwhat happens\b"],
            [], []),

    # Performance
    TagRule("performance", "Performance optimization",
            [r"\bperform", r"\boptimiz", r"\bslow\b", r"\bfast\b", r"\befficient\b", r"\bbottleneck\b"],
            [], []),
    TagRule("batch-processing", "Batch operations and bulk processing",
            [r"\bbatch\b", r"\bbulk\b", r"\bin parallel\b", r"\bconcurren"],
            [], []),
    TagRule("memory", "Memory usage and leaks",
            [r"\bmemory\b", r"\bleak\b", r"\bgarbage\b", r"\bheap\b", r"\bbuffer\b"],
            [], []),

    # Security
    TagRule("security", "Security concerns and vulnerabilities",
            [r"\bsecur", r"\bvulnerab", r"\bXSS\b", r"\binjection\b", r"\bsanitiz"],
            [], []),
    TagRule("auth", "Authentication and authorization",
            [r"\bauth", r"\btoken\b", r"\bsession\b", r"\bpermission\b", r"\brole\b", r"\bguard\b"],
            [r"auth", r"guard"], []),
    TagRule("input-sanitization", "Input sanitization and escaping",
            [r"\bsanitiz", r"\bescape\b", r"\bclean\b.*\binput\b"],
            [], []),

    # Testing
    TagRule("testing", "Testing practices and test quality",
            [r"\btest\b", r"\bspec\b", r"\bcoverage\b", r"\bassert", r"\bexpect\b"],
            [r"\.spec\.", r"\.test\."], []),
    TagRule("mocking", "Mocking and test doubles",
            [r"\bmock\b", r"\bstub\b", r"\bspy\b", r"\bfake\b"],
            [], []),

    # Types
    TagRule("types", "Type annotations and type safety",
            [r"\btype\b", r"\binterface\b", r"\bgeneric", r"\btypedef\b", r"\btype.?safe"],
            [r"\.types?\.", r"\.interface\."], []),
    TagRule("enums", "Enum usage and constants",
            [r"\benum\b", r"\bconstant\b", r"\bconst\b.*\bas const\b"],
            [r"\.enum\."], [r"export enum", r"as const"]),

    # Ops
    TagRule("logging", "Logging practices",
            [r"\blog\b", r"\blogger\b", r"\blogging\b", r"\bconsole\.log\b"],
            [], [r"logger\.", r"console\.log"]),
    TagRule("config", "Configuration management",
            [r"\bconfig\b", r"\benv\b", r"\benvironment\b", r"\bsetting"],
            [r"config", r"\.env"], []),
    TagRule("deployment", "Deployment and CI/CD",
            [r"\bdeploy", r"\bCI\b", r"\bCD\b", r"\bpipeline\b", r"\bdocker\b"],
            [r"Dockerfile", r"\.github/workflows"], []),

    # NestJS-specific
    TagRule("nestjs-modules", "NestJS module organization",
            [r"\b@Module\b", r"\bmodule\b.*\bimport", r"\bprovider", r"\bexport"],
            [r"\.module\."], [r"@Module"]),
    TagRule("nestjs-providers", "NestJS providers and services",
            [r"\b@Injectable\b", r"\bservice\b", r"\bprovider\b", r"\binteractor\b"],
            [r"\.service\.", r"\.interactor\."], [r"@Injectable"]),
    TagRule("mikroorm", "MikroORM usage and patterns",
            [r"\bMikroORM\b", r"\bEntity\b", r"\bRepository\b", r"\b(em|entityManager)\b", r"\bflush\b"],
            [r"\.entity\.", r"\.repository\."], [r"em\.", r"@Entity", r"EntityManager"]),
    TagRule("bullmq", "BullMQ job queues",
            [r"\bBullMQ\b", r"\bjob\b", r"\bqueue\b", r"\bworker\b", r"\bprocessor\b"],
            [r"\.processor\.", r"\.queue\."], [r"@Processor", r"@Process"]),

    # Patterns
    TagRule("guards", "Guards and access control",
            [r"\bguard\b", r"\b@UseGuards\b", r"\bcanActivate\b"],
            [r"\.guard\."], [r"@UseGuards", r"CanActivate"]),
    TagRule("interceptors", "Interceptors and middleware",
            [r"\binterceptor\b", r"\bmiddleware\b"],
            [r"\.interceptor\.", r"\.middleware\."], [r"@UseInterceptors"]),
    TagRule("dto-patterns", "DTO patterns and data transfer",
            [r"\bDTO\b", r"\bdata transfer\b", r"\bpayload\b"],
            [r"\.dto\."], []),
]


def tag_comments(conn, comment_ids: list[int], config: CrtkConfig,
                 batch_size: int = 200) -> int:
    """Apply keyword-based tags to comments. Returns number of tags applied."""
    # Pre-create all seed tags
    tag_id_cache: dict[str, int] = {}
    for rule in SEED_TAG_RULES:
        tag_id_cache[rule.tag_name] = ensure_tag(conn, rule.tag_name, rule.description)
    conn.commit()

    # Compile regex patterns
    compiled_rules: list[tuple[TagRule, list[re.Pattern], list[re.Pattern], list[re.Pattern]]] = []
    for rule in SEED_TAG_RULES:
        compiled_rules.append((
            rule,
            [re.compile(p, re.IGNORECASE) for p in rule.body_patterns],
            [re.compile(p, re.IGNORECASE) for p in rule.path_patterns],
            [re.compile(p, re.IGNORECASE) for p in rule.hunk_patterns],
        ))

    total_tags = 0
    for i in range(0, len(comment_ids), batch_size):
        batch_ids = comment_ids[i:i + batch_size]
        comments = get_comments_by_ids(conn, batch_ids)

        for comment in comments:
            matched_tags = _match_comment(comment, compiled_rules)
            for tag_name, confidence in matched_tags:
                tag_id = tag_id_cache[tag_name]
                add_comment_tag(conn, comment.id, tag_id, confidence)
                total_tags += 1

        conn.commit()
        logger.info("Tagged %d/%d comments", min(i + batch_size, len(comment_ids)), len(comment_ids))

    logger.info("Applied %d tags to %d comments", total_tags, len(comment_ids))
    return total_tags


def _match_comment(comment, compiled_rules) -> list[tuple[str, float]]:
    """Match a comment against all rules. Returns list of (tag_name, confidence)."""
    matches = []
    body = comment.body or ""
    path = comment.path or ""
    hunk = comment.diff_hunk or ""

    for rule, body_patterns, path_patterns, hunk_patterns in compiled_rules:
        score = 0.0

        for pattern in body_patterns:
            if pattern.search(body):
                score += 0.5
                break

        for pattern in path_patterns:
            if pattern.search(path):
                score += 0.3
                break

        for pattern in hunk_patterns:
            if pattern.search(hunk):
                score += 0.2
                break

        if score >= 0.3:  # At least one pattern category matched
            matches.append((rule.tag_name, min(score, 1.0)))

    return matches
