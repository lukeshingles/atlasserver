# Generated by Django 3.1.3 on 2020-11-23 09:16

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('forcephot', '0028_task_send_email'),
    ]

    operations = [
        migrations.AlterField(
            model_name='task',
            name='send_email',
            field=models.BooleanField(default=True, verbose_name='Email me when completed'),
        ),
        migrations.AlterField(
            model_name='task',
            name='timestamp',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
    ]