"""Run PostgreSQL schema migrations and backfill portal analytics."""
import secrets
from . import db, portal


def backfill_analytics() -> int:
    """Populate analytics tables for exchanges recorded before analytics grouping existed."""
    processed = 0
    with db._get_pool().connection() as connection:
        with connection.transaction():
            rows = connection.execute(
                "SELECT a.id, a.tenant_id, a.question, a.answer_status, a.created_at, m.content AS answer "
                "FROM chat_analytics a LEFT JOIN chat_messages m ON m.id=a.message_id ORDER BY a.created_at, a.id"
            ).fetchall()
            for row in rows:
                normalized = ' '.join(row['question'].lower().split())
                topic = portal._topic_for(row['question'])
                answer_status = 'unanswered' if "i don't have that information on the site" in (row['answer'] or '').lower() else row['answer_status']
                connection.execute(
                    "UPDATE chat_analytics SET normalized_question=%s, topic=%s, answer_status=%s, fallback_used=%s WHERE id=%s",
                    (normalized, topic, answer_status, answer_status != 'answered', row['id']),
                )

                group = connection.execute(
                    "SELECT id FROM question_groups WHERE tenant_id=%s AND group_name=%s FOR UPDATE",
                    (row['tenant_id'], normalized),
                ).fetchone()
                if not group:
                    group_id = secrets.token_urlsafe(18)
                    connection.execute(
                        "INSERT INTO question_groups "
                        "(id,tenant_id,group_name,representative_question,question_count,last_seen_at,created_at,updated_at) "
                        "VALUES (%s,%s,%s,%s,0,%s,%s,%s)",
                        (group_id, row['tenant_id'], normalized, row['question'], row['created_at'], row['created_at'], row['created_at']),
                    )
                    group = {'id': group_id}
                connection.execute(
                    "INSERT INTO question_group_members "
                    "(id,group_id,analytics_id,similarity_score,created_at) "
                    "VALUES (%s,%s,%s,1,%s) ON CONFLICT DO NOTHING",
                    (secrets.token_urlsafe(18), group['id'], row['id'], row['created_at']),
                )

                if answer_status != 'answered':
                    unanswered = connection.execute(
                        "SELECT id FROM unanswered_questions WHERE tenant_id=%s AND normalized_question=%s FOR UPDATE",
                        (row['tenant_id'], normalized),
                    ).fetchone()
                    if not unanswered:
                        connection.execute(
                            "INSERT INTO unanswered_questions "
                            "(id,tenant_id,analytics_id,question,normalized_question,frequency,suggested_action,first_seen_at,last_seen_at,updated_at) "
                            "VALUES (%s,%s,%s,%s,%s,0,'Review and add this answer to the knowledge base',%s,%s,%s)",
                            (secrets.token_urlsafe(18), row['tenant_id'], row['id'], row['question'], normalized, row['created_at'], row['created_at'], row['created_at']),
                        )

                connection.execute(
                    "INSERT INTO topic_analytics (id,tenant_id,analytics_id,topic_name,confidence_score,created_at) "
                    "SELECT %s,%s,%s,%s,1,%s WHERE NOT EXISTS "
                    "(SELECT 1 FROM topic_analytics WHERE analytics_id=%s)",
                    (secrets.token_urlsafe(18), row['tenant_id'], row['id'], topic, row['created_at'], row['id']),
                )
                processed += 1

            connection.execute(
                "UPDATE question_groups qg SET question_count=(SELECT count(*) FROM question_group_members members WHERE members.group_id=qg.id), "
                "last_seen_at=(SELECT max(analytics.created_at) FROM chat_analytics analytics JOIN question_group_members members ON members.analytics_id=analytics.id WHERE members.group_id=qg.id), "
                "updated_at=%s",
                (db.now(),),
            )
            connection.execute(
                "UPDATE unanswered_questions unanswered SET frequency=(SELECT count(*) FROM chat_analytics analytics WHERE analytics.tenant_id=unanswered.tenant_id AND analytics.normalized_question=unanswered.normalized_question AND analytics.answer_status <> 'answered'), "
                "last_seen_at=(SELECT max(analytics.created_at) FROM chat_analytics analytics WHERE analytics.tenant_id=unanswered.tenant_id AND analytics.normalized_question=unanswered.normalized_question AND analytics.answer_status <> 'answered'), updated_at=%s",
                (db.now(),),
            )
    return processed


if __name__ == "__main__":
    db.init_db()
    count = backfill_analytics()
    print(f"Database schema is ready; processed {count} analytics records.")
